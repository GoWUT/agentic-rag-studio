"""Per-process pooled engines, async transactions and legacy sync query port."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import re

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.engine import make_url
from server.db.schema import LEGACY_COLUMNS, metadata

REVISION = '5b2_0001'
MUTATION_LOCK = 5081001


class PersistenceUnavailable(RuntimeError):
    pass


def driver_url(url):
    value = make_url(url)
    if value.drivername not in {'postgresql', 'postgresql+psycopg'}:
        raise ValueError('DATABASE_URL must use PostgreSQL with psycopg 3')
    return value.set(drivername='postgresql+psycopg')


def psycopg_dsn(url):
    return driver_url(url).set(drivername='postgresql').render_as_string(hide_password=False)


def connection_options(url):
    return (str(driver_url(url).query.get('options', '')) + ' -c timezone=UTC').strip()


class PostgresRuntime:
    """Created once at composition; pools connect lazily and close at shutdown.

    Sync pool serves existing synchronous graph nodes/repositories in threads.
    Async pool/AsyncSession serves HTTP queue transactions and health.
    """
    backend = 'postgresql'

    def __init__(self, config):
        self.config = config
        url = driver_url(config.get('DATABASE_URL', ''))
        options = dict(pool_size=config.get('DB_POOL_SIZE', 5),
                       max_overflow=config.get('DB_MAX_OVERFLOW', 5),
                       pool_timeout=config.get('DB_POOL_TIMEOUT', 30),
                       pool_recycle=config.get('DB_POOL_RECYCLE', 1800),
                       pool_pre_ping=config.get('DB_POOL_PRE_PING', True),
                       hide_parameters=True, connect_args={'connect_timeout': 5, 'options': connection_options(config['DATABASE_URL'])})
        self.sync_engine = create_engine(url, **options)
        self.async_engine = create_async_engine(url, **options)
        self.sessions = async_sessionmaker(self.async_engine, expire_on_commit=False)

    async def startup(self):
        result = await self.health()
        if result != 'ok':
            raise PersistenceUnavailable('Database unavailable or application migration behind')

    async def health(self):
        try:
            async with self.async_engine.connect() as connection:
                revision = await connection.scalar(text('SELECT version_num FROM alembic_version'))
                return 'ok' if revision == REVISION else 'migration_behind'
        except Exception:
            return 'unavailable'

    async def close(self):
        await self.async_engine.dispose()
        self.sync_engine.dispose()

    @contextmanager
    def connect(self):
        with self.sync_engine.begin() as connection:
            yield RelationalConnection(connection)


class Rows:
    def __init__(self, result=None):
        self.rowcount = result.rowcount if result is not None else 0
        self.rows = []
        if result is not None and result.returns_rows:
            for row in result.mappings():
                self.rows.append(Record({k: normalize(v) for k,v in row.items() if k != '_order'}))
    def fetchone(self):
        return self.rows.pop(0) if self.rows else None
    def fetchall(self):
        result, self.rows = self.rows, []
        return result
    def __iter__(self):
        return iter(self.rows)


class Record(dict):
    def __getitem__(self, key):
        return list(self.values())[key] if isinstance(key, int) else super().__getitem__(key)


def normalize(value):
    if isinstance(value, (dict,list)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    return value


class RelationalConnection:
    """Bounded SQL dialect port for shared synchronous domain repositories.

    No schema initialization or driver substitution: Alembic owns PG DDL.
    Explicit application metadata defines insert columns and JSON/time casts.
    Legacy BEGIN IMMEDIATE maps to a short transaction advisory mutex, preserving
    existing read-modify-write guarantees across processes (never across LLM calls).
    """
    def __init__(self, connection):
        self.connection = connection

    def execute(self, sql, parameters=()):
        if sql.strip().upper() == 'BEGIN IMMEDIATE':
            return Rows(self.connection.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': MUTATION_LOCK}))
        if re.match(r'\s*(CREATE|ALTER|PRAGMA)\b', sql, re.I):
            raise RuntimeError('PostgreSQL schema must be initialized with Alembic')
        sql = re.sub(r'\browid\b', '_order', sql, flags=re.I)
        ignore = bool(re.match(r'\s*INSERT OR IGNORE', sql, re.I))
        sql = re.sub(r'INSERT OR IGNORE', 'INSERT', sql, flags=re.I)
        match = re.match(r'\s*INSERT INTO (\w+)\s+VALUES', sql, re.I)
        if match:
            name = match[1]
            sql = sql.replace(match[0], f'INSERT INTO {name} ({",".join(LEGACY_COLUMNS[name])}) VALUES', 1)
        if ignore:
            sql += ' ON CONFLICT DO NOTHING'
        names = []
        parts = sql.split('?')
        if len(parts)-1 != len(parameters):
            raise ValueError('SQL parameter count does not match')
        for index in range(len(parameters)):
            names.append(f'p{index}')
        sql = ''.join(p + (':' + names[i] if i < len(names) else '') for i,p in enumerate(parts))
        # psycopg text parameters need explicit casts for JSONB and TIMESTAMPTZ.
        table_match = re.search(r'(?:INSERT INTO|UPDATE|FROM)\s+(\w+)', sql, re.I)
        if table_match and table_match[1] in metadata.tables:
            schema = metadata.tables[table_match[1]]
            insert = re.search(r'INSERT INTO \w+\s*\((.*?)\)\s*VALUES\s*\((.*?)\)', sql, re.I|re.S)
            pairs = []
            if insert:
                pairs += list(zip([c.strip() for c in insert[1].split(',')], [v.strip() for v in insert[2].split(',')]))
            pairs += re.findall(r'(\w+)\s*=\s*(:p\d+)', sql)
            for field, bind in pairs:
                if field in schema.c and bind.startswith(':p'):
                    kind = str(schema.c[field].type)
                    if kind == 'JSONB':
                        sql = re.sub(re.escape(bind) + r'\b', f'CAST({bind} AS JSONB)', sql)
                    elif kind == 'DATETIME':
                        sql = re.sub(re.escape(bind) + r'\b', f'CAST({bind} AS TIMESTAMPTZ)', sql)
        result = self.connection.execute(text(sql), dict(zip(names, parameters)))
        return Rows(result)
