from alembic import context
from sqlalchemy import create_engine, pool
from server.config import CONFIG
from server.db.runtime import driver_url
from server.db.schema import metadata

config = context.config
url = driver_url(CONFIG.get('DATABASE_URL', ''))

if context.is_offline_mode():
    context.configure(url=url, target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(url, poolclass=pool.NullPool, hide_parameters=True)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=metadata)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()
