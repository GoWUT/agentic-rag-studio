"""Read-only SQLite export, validated atomic import and stable-field verification.

DATABASE_URL supplies the destination; it is never printed. Checkpoint and Chroma
internals are intentionally excluded. Run first with --dry-run on a backup copy.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from sqlalchemy import create_engine, select, insert, text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import DateTime
from sqlalchemy.pool import NullPool
from server.db.schema_v1 import metadata, LEGACY_COLUMNS
from server.db.runtime import driver_url, REVISION


class MigrationBlocked(ValueError):
    pass


ACTIVE = {'RUNNING','WAITING_USER','PAUSED','QUEUED','INTERRUPTED','REQUIRES_RECONCILIATION'}


def canonical(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {k: canonical(v) for k,v in value.items()}
    if isinstance(value, list):
        return [canonical(v) for v in value]
    return value


def checksum(rows, fields):
    data = [canonical({k:r.get(k) for k in fields}) for r in rows]
    return hashlib.sha256(json.dumps(sorted(data, key=lambda r: json.dumps(r, sort_keys=True)),
        sort_keys=True, separators=(',',':'), ensure_ascii=False).encode()).hexdigest()


def read_sources(paths):
    data, versions, warnings = {}, {}, []
    for path in paths:
        path = Path(path).resolve(strict=True)
        db = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            versions[path.name] = db.execute('PRAGMA user_version').fetchone()[0]
            for name, in db.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                if name not in metadata.tables:
                    warnings.append(f'{path.name}: ignored non-application table {name}')
                    continue
                if name in data:
                    raise MigrationBlocked(f'Duplicate source table {name}')
                data[name] = [dict(row) for row in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
            violations = list(db.execute('PRAGMA foreign_key_check'))
            if violations:
                raise MigrationBlocked('Source SQLite foreign key violations')
        finally:
            db.close()
    for name in metadata.tables:
        if name not in data:
            data[name] = []
            warnings.append(f'Source has no table {name}; treated as empty')
    return data, versions, warnings


def transform(data, warnings):
    result = {}
    for name, rows in data.items():
        schema = metadata.tables[name]
        result[name] = []
        for row in rows:
            value = {}
            for field in LEGACY_COLUMNS[name]:
                raw = row.get(field)
                column = schema.c[field]
                if raw is None and not column.nullable:
                    raise MigrationBlocked(f'{name}.{field}: missing required source value')
                if isinstance(column.type, JSONB) and raw is not None:
                    raw = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(column.type, DateTime) and raw is not None:
                    raw = datetime.fromisoformat(raw)
                    if not raw.tzinfo:
                        warnings.append(f'{name}.{field}: naive timestamp interpreted as UTC')
                        raw = raw.replace(tzinfo=timezone.utc)
                    raw = raw.astimezone(timezone.utc)
                value[field] = raw
            payload = value.get('payload')
            if isinstance(payload, dict):
                for field in ('id','status','workspace_id','task_id'):
                    if field in value and field in payload and value[field] != payload[field]:
                        raise MigrationBlocked(f'{name}: indexed {field} differs from payload')
                timestamp = payload.get('created_at') or payload.get('requested_at') or payload.get('started_at')
                if timestamp:
                    instant = datetime.fromisoformat(timestamp)
                    value['created_at'] = instant.replace(tzinfo=timezone.utc) if not instant.tzinfo else instant.astimezone(timezone.utc)
                if name == 'tasks':
                    for field in ('version','queue_job_id','queued_at','worker_started_at','attempt_count','execution_backend'):
                        if field in payload:
                            raw = payload[field]
                            value[field] = datetime.fromisoformat(raw) if raw and field.endswith('_at') else raw
            result[name].append(value)
    # Validate formal PostgreSQL relationships even if old SQLite lacked FKs.
    for schema in metadata.sorted_tables:
        for foreign in schema.foreign_keys:
            destination = foreign.column
            keys = {r[destination.name] for r in result[destination.table.name]}
            for row in result[schema.name]:
                raw = row[foreign.parent.name]
                if raw is not None and raw not in keys:
                    raise MigrationBlocked(f'{schema.name}.{foreign.parent.name}: orphan reference')
    return result


def write_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ['# PostgreSQL Migration Report', '', f"Mode: {report['mode']}",
        f"Outcome: {report['outcome']}", f"Source schema versions: {report['source_versions']}",
        f"Destination revision: {report.get('destination_revision')}",
        f"Foreign key validation: {report['foreign_keys']}", '',
        '| Table | Source rows | Destination rows | Verified | Source checksum |',
        '| --- | ---: | ---: | --- | --- |']
    for name, row in report['tables'].items():
        lines.append(f"| {name} | {row['source_rows']} | {row.get('destination_rows','—')} | {row.get('verified','NOT RUN')} | {row.get('source_checksum','—')} |")
    lines += ['', 'Warnings:', *['- '+w for w in report['warnings']]]
    path.write_text('\n'.join(lines)+'\n', encoding='utf-8')


def migrate(paths, url, *, dry_run=False, allow_non_empty=False, report_path=None):
    report = dict(mode='dry-run' if dry_run else 'import', outcome='BLOCKED', source_versions={},
                  foreign_keys='NOT RUN', tables={}, warnings=[])
    engine = None
    try:
        source, versions, warnings = read_sources(paths)
        report.update(source_versions=versions, warnings=warnings,
            tables={name: {'source_rows':len(rows)} for name,rows in source.items()})
        rows = transform(source, warnings)
        report['foreign_keys'] = 'PASS'
        active = [r for r in rows['tasks'] if r['status'] in ACTIVE]
        if active:
            warnings.append(f'{len(active)} active legacy tasks: complete/cancel active tasks first; checkpoint cutover blocked')
        engine = create_engine(driver_url(url), poolclass=NullPool, hide_parameters=True)
        with engine.begin() as connection:
            revision = connection.scalar(text('SELECT version_num FROM alembic_version'))
            report['destination_revision'] = revision
            if revision != REVISION:
                raise MigrationBlocked('Destination application schema is behind; apply Alembic')
            if not dry_run:
                connection.execute(text('LOCK TABLE ' + ','.join('"'+t.name+'"' for t in metadata.sorted_tables) + ' IN ACCESS EXCLUSIVE MODE'))
            before = {t.name: connection.scalar(select(func.count()).select_from(t)) for t in metadata.sorted_tables}
            for name in rows:
                fields = LEGACY_COLUMNS[name]
                report['tables'][name].update(destination_rows=before[name], source_checksum=checksum(rows[name],fields))
            if dry_run:
                report['outcome'] = 'BLOCKED' if active else 'DRY RUN VALIDATED'
                if any(before.values()):
                    warnings.append('Destination is non-empty; import requires --allow-non-empty and collision-free IDs')
            else:
                if active:
                    raise MigrationBlocked('Complete/cancel active tasks before cutover')
                if any(before.values()) and not allow_non_empty:
                    raise MigrationBlocked('Destination contains business data; default is ABORT')
                for schema in metadata.sorted_tables:
                    if rows[schema.name]:
                        # Per-row defaults may differ. All writes remain one transaction.
                        for row in rows[schema.name]:
                            connection.execute(insert(schema).values(**row))
                for schema in metadata.sorted_tables:
                    name = schema.name
                    count = connection.scalar(select(func.count()).select_from(schema))
                    key = list(schema.primary_key.columns)[0]
                    imported = [dict(r) for r in connection.execute(select(schema).where(key.in_([r[key.name] for r in rows[name]]))).mappings()]
                    verified = count == before[name]+len(rows[name]) and checksum(imported, LEGACY_COLUMNS[name]) == checksum(rows[name], LEGACY_COLUMNS[name])
                    report['tables'][name].update(destination_rows=count, verified='PASS' if verified else 'FAIL')
                    if not verified:
                        raise MigrationBlocked('Row count or checksum verification failed')
                report['outcome'] = 'VERIFIED'
        return report
    except Exception as error:
        # Never expose destination URL, credentials, payload bind parameters or traceback.
        report['warnings'].append(str(error) if isinstance(error, MigrationBlocked) else type(error).__name__ + ': import aborted and rolled back')
        raise MigrationBlocked(report['warnings'][-1]) from None
    finally:
        if engine:
            engine.dispose()
        if report_path:
            write_report(report_path, report)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sqlite', required=True)
    parser.add_argument('--index-sqlite')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--allow-non-empty', action='store_true')
    parser.add_argument('--report', default='docs/generated/POSTGRES_MIGRATION_REPORT.md')
    args = parser.parse_args()
    try:
        from dotenv import load_dotenv
        load_dotenv()
        report = migrate([p for p in [args.sqlite,args.index_sqlite] if p], os.getenv('DATABASE_URL',''),
            dry_run=args.dry_run, allow_non_empty=args.allow_non_empty, report_path=args.report)
        print(report['outcome'])
        print('Report: ' + args.report)
        sys.exit(0 if report['outcome'] != 'BLOCKED' else 2)
    except MigrationBlocked as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
