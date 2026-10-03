# Phase 5B-1 migration and rollback

SQLite/inline defaults remain unchanged. Do not overwrite an existing `.env`.
Choose a new empty PostgreSQL database and configure the following in the shell
or your own deployment configuration; credentials are operator-provided:

```text
DATABASE_BACKEND=postgresql
DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE
TASK_EXECUTION_MODE=worker
TASK_QUEUE_ENABLED=true
```

## Procedure

1. Stop legacy task producers and complete/cancel RUNNING, PAUSED, WAITING_USER,
   QUEUED, INTERRUPTED and REQUIRES_RECONCILIATION tasks. Migration rejects these
   resumable states because old SDK checkpoint internals are not migrated.
2. Back up both `WORKSPACE_DIR/sessions.sqlite3` and `indexes.sqlite3`. Use the
   SQLite backup API for a consistent snapshot when WAL is enabled. Back up the
   associated filesystem assets separately. Never delete the original stores.
3. Start only local PostgreSQL, if needed:

   ```bash
   docker compose -f docker-compose.postgres.yml up -d
   ```

   Set POSTGRES_PASSWORD outside source control. The compose definition binds
   loopback and uses a named volume. Do not use `down -v` on valuable data.
4. Install locked dependencies without removing separately installed OCR tooling:

   ```bash
   uv sync --inexact --extra ocr
   ```

   The OCR extra is optional; omit it when not used. `--inexact` preserves extra
   tooling packages already in the local virtual environment.
5. Apply application schema:

   ```bash
   uv run --no-sync alembic upgrade head
   ```

6. Initialize official queue/checkpoint schemas (first setup):

   ```bash
   uv run --no-sync python -m server.worker --setup
   ```

   Queue initialization is skipped when its table already exists; this command
   is not an automatic queue-library upgrade. For subsequent Procrastinate
   upgrades, use its official migration path. Saver setup is SDK idempotent.
7. Validate the backups without writing the destination:

   ```bash
   uv run --no-sync python scripts/migrate_sqlite_to_postgres.py --sqlite /path/to/backup/sessions.sqlite3 --index-sqlite /path/to/backup/indexes.sqlite3 --dry-run
   ```

   Report: table rows, FK validation, source user_version, target Alembic revision,
   stable-field checksums, active-task warnings. Active tasks yield BLOCKED even
   in dry-run. No queue/checkpoint/Chroma tables are copied.
8. Import the same backups:

   ```bash
   uv run --no-sync python scripts/migrate_sqlite_to_postgres.py --sqlite /path/to/backup/sessions.sqlite3 --index-sqlite /path/to/backup/indexes.sqlite3
   ```

   Default ABORT for any non-empty target. `--allow-non-empty` only permits
   collision-free inserts; it never updates existing records or overwrites IDs.
   One destination transaction locks business tables, inserts in FK order and
   verifies row-count deltas and all stable source columns before committing.
   Failure rolls back the entire import. Migration requires exclusive maintenance
   access and can hold table locks; do not run it against an active deployment.
9. Inspect `docs/generated/POSTGRES_MIGRATION_REPORT.md`: every application table
   must show verified PASS. Copy file assets separately only if changing hosts;
   migration preserves file_path, index directories, fingerprints and metadata.
10. Launch API, worker and UI with the **same** database and WORKSPACE_DIR:

    ```bash
    uv run --no-sync python -m server.api
    uv run --no-sync python -m server.worker
    uv run --no-sync streamlit run client/app.py
    ```

    Check `/health/ready`, `/health` and a synthetic task. `/run` and `/resume`
    return 202; `/tasks/{id}` and events expose durable state.

## Rollback

Stop PostgreSQL producers/workers at safe boundaries and record any new jobs or
business data created after cutover. Switch DATABASE_BACKEND=sqlite,
TASK_EXECUTION_MODE=inline, TASK_QUEUE_ENABLED=false with the original WORKSPACE_DIR.
Restart the API against the preserved original SQLite checkpoint/application
stores. PostgreSQL data is retained for investigation/export.

**PostgreSQL changes are not automatically copied back to SQLite.** Returning to
the original store does not include post-cutover tasks, sessions, memory or events.
Manual reconciliation is required if any new production data was written.
An Alembic downgrade is not this rollback procedure and drops application tables;
it was exercised only in a fresh empty synthetic database.
