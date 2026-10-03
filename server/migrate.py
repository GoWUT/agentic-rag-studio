"""Fail-fast one-shot business, queue and checkpoint schema initialization."""
import asyncio
from alembic.config import Config
from alembic import command


def main():
    command.upgrade(Config('alembic.ini'),'head')
    from server.worker import main as initialize_worker
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(initialize_worker(setup=True))


if __name__=='__main__':main()
