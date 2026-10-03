"""SDK-only checkpoint selection and sync graph bridge to AsyncPostgresSaver."""
import asyncio
from threading import Thread, Event
from langgraph.checkpoint.base import BaseCheckpointSaver


class EventLoopPortal:
    """Own the async saver on one loop; sync graph threads submit public SDK calls."""
    def __init__(self):
        self.loop = asyncio.SelectorEventLoop()
        ready = Event()
        def run():
            asyncio.set_event_loop(self.loop)
            ready.set()
            self.loop.run_forever()
        self.thread = Thread(target=run, name='checkpoint-sdk', daemon=True)
        self.thread.start()
        ready.wait()

    def call(self, coroutine):
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result()

    def close(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=10)
        self.loop.close()


class PostgresCheckpointBridge(BaseCheckpointSaver):
    def __init__(self, runtime, serde):
        super().__init__(serde=serde)
        self.runtime = runtime
        self.portal = self.pool = self.saver = None

    def open(self, *, setup=False):
        if self.portal:
            return
        from psycopg_pool import AsyncConnectionPool
        from psycopg.rows import dict_row
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from server.db.runtime import psycopg_dsn, connection_options
        self.portal = EventLoopPortal()
        async def initialize():
            self.pool = AsyncConnectionPool(psycopg_dsn(self.runtime.config['DATABASE_URL']),
                min_size=1, max_size=2, open=False,
                kwargs={'autocommit': True, 'prepare_threshold': 0, 'row_factory': dict_row,
                        'options': connection_options(self.runtime.config['DATABASE_URL'])})
            await self.pool.open(wait=True)
            self.saver = AsyncPostgresSaver(self.pool, serde=self.serde)
            if setup:
                await self.saver.setup()
            else:
                async with self.pool.connection() as connection:
                    await connection.execute('SELECT 1 FROM checkpoint_migrations LIMIT 1')
        try:
            self.portal.call(initialize())
        except Exception:
            self.close()
            raise

    def get_tuple(self, config):
        return self.portal.call(self.saver.aget_tuple(config))

    def list(self, config, *, filter=None, before=None, limit=None):
        async def collect():
            return [v async for v in self.saver.alist(config, filter=filter, before=before, limit=limit)]
        yield from self.portal.call(collect())

    def put(self, config, checkpoint, metadata, new_versions):
        return self.portal.call(self.saver.aput(config, checkpoint, metadata, new_versions))

    def put_writes(self, config, writes, task_id, task_path=''):
        return self.portal.call(self.saver.aput_writes(config, writes, task_id, task_path))

    def delete_thread(self, thread_id):
        return self.portal.call(self.saver.adelete_thread(thread_id))

    async def aget_tuple(self, config):
        return await asyncio.to_thread(self.get_tuple, config)

    async def alist(self, config, **kwargs):
        for value in await asyncio.to_thread(lambda: list(self.list(config, **kwargs))):
            yield value

    async def aput(self, config, checkpoint, metadata, new_versions):
        return await asyncio.to_thread(self.put, config, checkpoint, metadata, new_versions)

    async def aput_writes(self, config, writes, task_id, task_path=''):
        return await asyncio.to_thread(self.put_writes, config, writes, task_id, task_path)

    def close(self):
        if self.pool and self.portal:
            self.portal.call(self.pool.close())
        if self.portal:
            self.portal.close()
        self.portal = self.pool = self.saver = None


class CheckpointFactory:
    @staticmethod
    def create(source, serde):
        if getattr(source, 'backend', 'sqlite') == 'postgresql':
            return PostgresCheckpointBridge(source, serde)
        from server.phase4 import ManagedSqliteSaver
        saver = ManagedSqliteSaver(source, serde)
        with saver.cursor():
            pass
        return saver
