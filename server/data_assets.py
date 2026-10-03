"""Data assets, execution records and artifacts with ID-based access."""
import hashlib
import json
import mimetypes
from pathlib import Path
import zipfile
from typing import Any

from pydantic import BaseModel, Field
from server.persistence import Database, RecordNotFound, encode, identity, now


class DataAsset(BaseModel):
    id: str = Field(default_factory=identity)
    workspace_id: str
    filename: str
    display_name: str
    file_type: str
    mime_type: str
    file_path: str
    fingerprint: str
    size_bytes: int
    status: str = 'ready'
    row_count: int = 0
    column_count: int = 0
    schema_metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)


class Artifact(BaseModel):
    id: str = Field(default_factory=identity)
    task_id: str | None = None
    step_id: str | None = None
    execution_id: str
    workspace_id: str
    filename: str
    artifact_type: str
    mime_type: str
    file_path: str
    size_bytes: int
    created_at: str = Field(default_factory=now)
    metadata: dict[str, Any] = Field(default_factory=dict)


def read_tables(path):
    import pandas as pd
    path = Path(path)
    if path.suffix.lower() == '.csv':
        return {'data': pd.read_csv(path, encoding='utf-8-sig')}
    if path.suffix.lower() == '.xlsx':
        with zipfile.ZipFile(path) as archive:
            if sum(info.file_size for info in archive.infolist()) > 200*1024*1024:
                raise ValueError('Excel expanded size limit exceeded')
        return pd.read_excel(path, sheet_name=None, engine='openpyxl')
    if path.suffix.lower() == '.json':
        value = json.loads(path.read_text(encoding='utf-8-sig'))
        if isinstance(value, list) and all(isinstance(row, dict) for row in value):
            return {'data': pd.DataFrame.from_records(value)}
        if isinstance(value, dict) and value and all(isinstance(v, list) for v in value.values()):
            return {'data': pd.DataFrame(value)}
        raise ValueError('Unsupported JSON shape; use records or a tabular object')
    raise ValueError('Unsupported dataset type')


def describe_tables(tables):
    result = {}
    for name, frame in tables.items():
        if len(frame) > 200000 or len(frame.columns) > 1000:
            raise ValueError('Dataset shape limit exceeded (200000 rows / 1000 columns)')
        result[name] = {'rows': len(frame), 'columns': len(frame.columns), 'column_names': [str(x) for x in frame.columns],
                        'dtypes': {str(k): str(v) for k,v in frame.dtypes.items()},
                        'missing': {str(k): int(v) for k,v in frame.isna().sum().items()},
                        'statistics': json.loads(frame.describe(include='all').to_json()) if len(frame.columns) else {},
                        'sample': json.loads(frame.head(5).to_json(orient='records'))}
    return {'sheet_names': list(result), 'sheets': result}


class DataStore(Database):
    MIME = {'.csv': {'text/csv', 'application/csv', 'application/vnd.ms-excel', 'application/octet-stream'},
            '.xlsx': {'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'application/octet-stream'},
            '.json': {'application/json', 'text/json', 'application/octet-stream'}}

    def __init__(self, path, root, config=None):
        super().__init__(path)
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.config = config or {}
        if self.backend == 'postgresql':
            return  # Application DDL is owned by Alembic.
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS data_assets (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(workspace_id,fingerprint))')
            db.execute('CREATE INDEX IF NOT EXISTS dataset_workspace ON data_assets(workspace_id)')
            db.execute('CREATE TABLE IF NOT EXISTS analysis_executions (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, task_id TEXT, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, task_id TEXT, execution_id TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS artifact_task ON artifacts(task_id)')

    def upload(self, workspace_id, filename, stream, mime_type='application/octet-stream'):
        name = Path(filename.replace('\\','/')).name
        suffix = Path(name).suffix.lower()
        if suffix not in self.MIME or mime_type.split(';')[0] not in self.MIME[suffix]:
            raise ValueError('Unsupported dataset extension or MIME type')
        content = stream.read(self.config.get('DATASET_MAX_FILE_MB', 50)*1024*1024+1)
        if not content or len(content) > self.config.get('DATASET_MAX_FILE_MB', 50)*1024*1024:
            raise ValueError('Empty dataset or upload size limit exceeded')
        fingerprint = hashlib.sha256(content).hexdigest()
        for asset in self.list(workspace_id):
            if asset.fingerprint == fingerprint:
                return asset
        folder = self.root/'datasets'
        folder.mkdir(exist_ok=True)
        path = folder/(identity()+suffix)
        path.write_bytes(content)
        try:
            inspected = describe_tables(read_tables(path))
        except Exception as error:
            path.unlink(missing_ok=True)
            raise ValueError('Dataset parsing failed: '+type(error).__name__ if not isinstance(error, ValueError) else str(error)) from error
        first = next(iter(inspected['sheets'].values()), {'rows':0,'columns':0})
        record = DataAsset(workspace_id=workspace_id, filename=name, display_name=name, file_type=suffix[1:],
                           mime_type=mimetypes.guess_type(name)[0] or mime_type, file_path=str(path), fingerprint=fingerprint,
                           size_bytes=len(content), row_count=first['rows'], column_count=first['columns'], schema_metadata=inspected)
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO data_assets VALUES (?,?,?,?)', (record.id,workspace_id,fingerprint,record.model_dump_json()))
            row = db.execute('SELECT payload FROM data_assets WHERE workspace_id=? AND fingerprint=?', (workspace_id,fingerprint)).fetchone()
        saved = DataAsset.model_validate_json(row[0])
        if saved.id != record.id:
            path.unlink(missing_ok=True)
        return saved

    def list(self, workspace_id):
        with self.connect() as db:
            return [DataAsset.model_validate_json(row[0]) for row in db.execute('SELECT payload FROM data_assets WHERE workspace_id=? ORDER BY rowid', (workspace_id,))]

    def get(self, workspace_id, dataset_id):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM data_assets WHERE id=? AND workspace_id=?', (dataset_id,workspace_id)).fetchone()
        if not row:
            raise RecordNotFound(dataset_id)
        asset = DataAsset.model_validate_json(row[0])
        self.checked_path(asset.file_path)
        return asset

    def checked_path(self, path):
        target = Path(path).resolve()
        if not target.is_relative_to(self.root) or not target.is_file() or Path(path).is_symlink():
            raise ValueError('Managed file is missing or outside the runtime directory')
        return target

    def inspect(self, workspace_id, dataset_id):
        return self.get(workspace_id,dataset_id).schema_metadata

    def delete(self, workspace_id, dataset_id):
        self.get(workspace_id,dataset_id)
        with self.connect() as db:
            db.execute('DELETE FROM data_assets WHERE id=? AND workspace_id=?', (dataset_id,workspace_id))
        # Execution copies and previous lineage remain available; no shared files removed.

    def save_execution(self, result):
        with self.connect() as db:
            db.execute('INSERT INTO analysis_executions VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (result['execution_id'],result['workspace_id'],result.get('task_id'),encode(result)))

    def register_artifact(self, artifact):
        self.checked_path(artifact.file_path)
        with self.connect() as db:
            db.execute('INSERT INTO artifacts VALUES (?,?,?,?,?)', (artifact.id,artifact.workspace_id,artifact.task_id,artifact.execution_id,artifact.model_dump_json()))
        return artifact

    def artifact(self, artifact_id):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM artifacts WHERE id=?', (artifact_id,)).fetchone()
        if not row:
            raise RecordNotFound(artifact_id)
        value = Artifact.model_validate_json(row[0])
        self.checked_path(value.file_path)
        return value

    def artifacts(self, task_id):
        with self.connect() as db:
            return [Artifact.model_validate_json(row[0]) for row in db.execute('SELECT payload FROM artifacts WHERE task_id=?', (task_id,))]
