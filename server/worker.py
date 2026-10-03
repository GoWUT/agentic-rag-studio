"""Standalone worker: python -m server.worker [--setup|--recover-only]."""
import argparse
import asyncio
import os
import signal


async def main(setup=False, recover_only=False):
    from server.config import CONFIG
    from server.sessions import AgentSessionManager
    from server.runtime_services import configure_runtime, startup_runtime, shutdown_runtime
    from server.worker_runtime import recover_stalled
    if CONFIG.get('DATABASE_BACKEND') != 'postgresql' or CONFIG.get('TASK_EXECUTION_MODE') != 'worker':
        raise ValueError('Worker requires PostgreSQL worker mode')
    manager = AgentSessionManager(CONFIG)
    configure_runtime(manager)
    queue = manager.task_queue
    if setup:
        await queue.open()
        try:
            from sqlalchemy import text
            async with manager.database.sessions() as session:
                present = await session.scalar(text("SELECT to_regclass('procrastinate_jobs')"))
            if not present:
                await queue.app.schema_manager.apply_schema_async()
            if manager.phase4:
                await asyncio.to_thread(manager.phase4.checkpointer.open, setup=True)
        finally:
            await shutdown_runtime(manager)
        return
    await startup_runtime(manager)
    try:
        if CONFIG.get('WORKER_STALLED_JOB_RECOVERY',True):
            await recover_stalled(queue)
        if recover_only:
            return
        @queue.app.periodic(cron='* * * * *')
        @queue.app.task(name='agent.recover', queue=CONFIG.get('WORKER_QUEUE','agent'), queueing_lock='agent.recover')
        async def recovery(timestamp):
            if CONFIG.get('WORKER_STALLED_JOB_RECOVERY', True):
                await recover_stalled(queue)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        # An optional operator stop file makes graceful shutdown testable on
        # Windows without Unix signals. It carries no business task payload.
        async def operator_stop():
            path = os.getenv('WORKER_STOP_FILE')
            from pathlib import Path
            while not stop.is_set():
                if path and Path(path).exists():
                    stop.set()
                    return
                await asyncio.sleep(.5)
        watcher = asyncio.create_task(operator_stop())
        from procrastinate.worker import Worker
        worker = Worker(app=queue.app,
            queues=[CONFIG.get('WORKER_QUEUE','agent')], concurrency=CONFIG.get('WORKER_CONCURRENCY',2),
            install_signal_handlers=False, name='agent-worker-' + str(os.getpid()),
            shutdown_graceful_timeout=CONFIG.get('WORKER_GRACEFUL_SHUTDOWN_SECONDS',120),
            update_heartbeat_interval=10, stalled_worker_timeout=30)
        running = asyncio.create_task(worker.run())
        async def probe_identity():
            # A process-specific SDK ID keeps a healthy peer from masking this
            # worker's failure. The probe receipt lives on tmpfs, not business data.
            import json
            from pathlib import Path
            path=os.getenv('WORKER_HEALTH_FILE')
            if not path:return
            path=Path(path)
            try:
                while not running.done():
                    if worker.worker_id is not None:
                        path.write_text(json.dumps({'worker_id':worker.worker_id}),encoding='utf-8')
                    await asyncio.sleep(1)
            finally:
                path.unlink(missing_ok=True)
        probe=asyncio.create_task(probe_identity())
        stopping = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait([running, stopping], return_when=asyncio.FIRST_COMPLETED)
        if stopping in done:
            # Official worker stop hook requests graceful draining.
            worker.stop()
            await asyncio.wait_for(asyncio.shield(running), CONFIG.get('WORKER_GRACEFUL_SHUTDOWN_SECONDS',120))
        else:
            await running
        stopping.cancel()
        watcher.cancel()
        probe.cancel()
        await asyncio.gather(stopping, watcher, probe, return_exceptions=True)
    finally:
        await shutdown_runtime(manager)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--setup', action='store_true')
    parser.add_argument('--recover-only', action='store_true')
    args = parser.parse_args()
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(main(args.setup, args.recover_only))
