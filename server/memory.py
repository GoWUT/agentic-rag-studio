"""Scoped long-term memory, distinct from messages and factual evidence."""
import hashlib
import logging
import math
import re
from typing import Any, Literal
import unicodedata

from pydantic import BaseModel, Field
from server.persistence import Database, RecordNotFound, encode, identity, now
import json

LOGGER = logging.getLogger(__name__)


class MemoryCandidate(BaseModel):
    should_store: bool = False
    memory_type: Literal['preference', 'project', 'decision', 'instruction', 'episodic'] = 'project'
    content: str = Field(default='', max_length=3000)
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=0.75, ge=0, le=1)
    scope_type: Literal['user', 'workspace'] = 'user'
    reason: str = Field(default='', max_length=160)
    conflict_key: str = Field(default='', max_length=100)


class MemoryCandidates(BaseModel):
    candidates: list[MemoryCandidate] = Field(default_factory=list, max_length=6)


class LongTermMemory(BaseModel):
    id: str = Field(default_factory=identity)
    owner_id: str = 'local_default'
    scope_type: Literal['user', 'workspace'] = 'user'
    scope_id: str = 'local_default'
    memory_type: Literal['preference', 'project', 'decision', 'instruction', 'episodic']
    content: str = Field(min_length=1, max_length=3000)
    summary: str = ''
    importance: float = Field(default=0.5, ge=0, le=1)
    confidence: float = Field(default=1, ge=0, le=1)
    source_session_id: str | None = None
    source_task_id: str | None = None
    source_message_id: str | None = None
    content_hash: str = ''
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    last_accessed_at: str | None = None
    access_count: int = 0
    status: Literal['active', 'superseded'] = 'active'
    superseded_by: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


def sensitive(text):
    patterns = [r'(?i)(api[_ -]?key|password|authorization|bearer|secret|(?:access[_ -]?)?token|密码|密钥|访问凭证)\s*(?:[:=：]|is\b|为|是|\s)\s*\S+',
                r'\bsk-[\w-]{8,}', r'-----BEGIN .*PRIVATE KEY', r'\b\d{17}[\dXx]\b',
                r'\b(?:\d[ -]?){13,19}\b', r'(?i)\b(?:ghp_|gho_|AKIA)[a-z0-9]{12,}',
                r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+']
    return any(re.search(pattern, text) for pattern in patterns)


def normalized(text):
    return re.sub(r'[^\w]', '', unicodedata.normalize('NFKC', text).casefold())


def preference_key(content):
    lower = content.casefold()
    if re.search(r'chinese|中文', lower) and re.search(r'prefer|language|回答|使用|偏好', lower):
        return 'preferred_language', 'zh'
    if re.search(r'english|英文', lower) and re.search(r'prefer|language|回答|使用|偏好', lower):
        return 'preferred_language', 'en'
    if re.search(r'concise|简洁|简短', lower):
        return 'response_detail', 'concise'
    if re.search(r'detailed|详细', lower):
        return 'response_detail', 'detailed'
    if re.search(r'markdown', lower) and re.search(r'format|格式|prefer|偏好', lower):
        return 'report_format', 'markdown'
    return '', ''


def cosine(a, b):
    if len(a) != len(b) or not a:
        return 0.0
    norm = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b))
    return sum(x*y for x,y in zip(a,b)) / norm if norm else 0.0


class MemoryStore(Database):
    def __init__(self, path, config=None, embedder=None):
        super().__init__(path)
        self.config = config or {}
        self.embedder = embedder
        if self.backend == 'postgresql':
            return  # Application DDL is owned by Alembic.
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS long_term_memories (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, scope_type TEXT NOT NULL, scope_id TEXT NOT NULL, memory_type TEXT NOT NULL, status TEXT NOT NULL, content_hash TEXT NOT NULL, payload TEXT NOT NULL, embedding TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS memory_scope_status ON long_term_memories(owner_id, scope_type, scope_id, status)')
            db.execute('CREATE INDEX IF NOT EXISTS memory_hash ON long_term_memories(owner_id, scope_id, content_hash)')

    def _vector(self, text):
        try:
            if self.embedder is None and self.config.get('EMBEDDING_MODEL'):
                from server.rag.embeddings import get_embedder
                self.embedder = get_embedder(self.config['EMBEDDING_MODEL'])
            return list(map(float, self.embedder.embed_query(text))) if self.embedder else []
        except Exception as error:
            LOGGER.warning('Memory embedding unavailable error_type=%s', type(error).__name__)
            return []

    def list(self, *, owner_id='local_default', workspace_id=None, memory_type=None, status='active', include_workspace=False):
        with self.connect() as db:
            if include_workspace and workspace_id:
                rows=db.execute("SELECT payload FROM long_term_memories WHERE (owner_id=? AND scope_type='user') OR (scope_type='workspace' AND scope_id=?) ORDER BY rowid DESC",(owner_id,workspace_id)).fetchall()
            else:
                rows = db.execute('SELECT payload FROM long_term_memories WHERE owner_id=? ORDER BY rowid DESC', (owner_id,)).fetchall()
        values = [LongTermMemory.model_validate_json(row[0]) for row in rows]
        return [m for m in values if (status is None or m.status == status) and (memory_type is None or m.memory_type == memory_type)
                and (m.scope_type == 'user' or m.scope_id == workspace_id)]

    def get(self, memory_id, owner_id='local_default'):
        with self.connect() as db:
            row = db.execute('SELECT payload FROM long_term_memories WHERE id=? AND owner_id=?', (memory_id, owner_id)).fetchone()
        if not row:
            raise RecordNotFound(memory_id)
        return LongTermMemory.model_validate_json(row[0])

    def create(self, memory: LongTermMemory, *, automatic=False, replace_id=None):
        memory.content = memory.content.strip()
        if not memory.content or sensitive(memory.content):
            raise ValueError('Sensitive or empty memory rejected')
        if automatic and (memory.confidence < self.config.get('MEMORY_MIN_CONFIDENCE', .75) or memory.importance < self.config.get('MEMORY_MIN_IMPORTANCE', .5)):
            return None
        if memory.scope_type == 'user':
            memory.scope_id = memory.owner_id
        key, value = preference_key(memory.content) if memory.memory_type == 'preference' else ('', '')
        memory.metadata = {**memory.metadata, **({'conflict_key': key, 'preference_value': value} if key else {})}
        memory.content_hash = hashlib.sha256(normalized(memory.content).encode()).hexdigest()
        vector = self._vector(memory.content)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            rows = db.execute('SELECT payload, embedding FROM long_term_memories WHERE owner_id=? AND scope_type=? AND scope_id=? AND status=?', (memory.owner_id, memory.scope_type, memory.scope_id, 'active')).fetchall()
            conflicts = []
            for row in rows:
                old = LongTermMemory.model_validate_json(row['payload'])
                if old.id == replace_id:
                    conflicts.append(old)
                    continue
                same_key = memory.metadata.get('conflict_key') and memory.metadata.get('conflict_key') == old.metadata.get('conflict_key')
                if old.content_hash == memory.content_hash or (same_key and value and value == old.metadata.get('preference_value')):
                    return old
                if same_key:
                    conflicts.append(old)
                elif old.memory_type == memory.memory_type and cosine(vector, json.loads(row['embedding'])) >= self.config.get('MEMORY_DEDUP_SIMILARITY', .94):
                    return old
            db.execute('INSERT INTO long_term_memories VALUES (?,?,?,?,?,?,?,?,?)', (memory.id, memory.owner_id, memory.scope_type, memory.scope_id, memory.memory_type, memory.status, memory.content_hash, memory.model_dump_json(), encode(vector)))
            for old in conflicts:
                old.status, old.superseded_by, old.updated_at = 'superseded', memory.id, now()
                db.execute('UPDATE long_term_memories SET status=?,payload=? WHERE id=?', (old.status, old.model_dump_json(), old.id))
        return memory

    def update(self, memory_id, content, owner_id='local_default'):
        old = self.get(memory_id, owner_id)
        # Updates preserve history and apply the same policy; unsafe input leaves old active.
        new = old.model_copy(update={'id': identity(), 'content': content, 'created_at': now(), 'updated_at': now(), 'status': 'active', 'superseded_by': None})
        saved = self.create(new, replace_id=old.id)
        if saved.id != old.id:
            old.status, old.superseded_by, old.updated_at = 'superseded', saved.id, now()
            with self.connect() as db:
                db.execute('UPDATE long_term_memories SET status=?,payload=? WHERE id=?', (old.status, old.model_dump_json(), old.id))
        return saved

    def delete(self, memory_id, owner_id='local_default'):
        self.get(memory_id, owner_id)
        with self.connect() as db:
            db.execute('DELETE FROM long_term_memories WHERE id=?', (memory_id,))

    def retrieve_memories(self, query, workspace_id=None, owner_id='local_default', include_workspace=False):
        candidates = self.list(owner_id=owner_id, workspace_id=workspace_id, include_workspace=include_workspace)
        if not candidates:
            return []
        vector = self._vector(query)
        with self.connect() as db:
            allowed={m.id for m in candidates}
            embeddings = {r['id']: json.loads(r['embedding']) for r in db.execute('SELECT id,embedding FROM long_term_memories WHERE status=?', ('active',)) if r['id'] in allowed}
        query_terms = set(re.findall(r'\w+', query.casefold()))
        scored = []
        for memory in candidates:
            similarity = cosine(vector, embeddings.get(memory.id, []))
            lexical = len(query_terms & set(re.findall(r'\w+', memory.content.casefold()))) / max(1, len(query_terms))
            relevant = max(similarity, lexical)
            # Stable preferences/instructions are continuity context even without keyword overlap.
            if relevant < .2 and memory.memory_type not in {'preference', 'instruction'}:
                continue
            from datetime import datetime, timezone
            age = max(0, (datetime.now(timezone.utc) - datetime.fromisoformat(memory.updated_at)).total_seconds()/86400)
            scored.append((.7*relevant + .2*memory.importance + .1/(1+age/30), memory))
        scored.sort(key=lambda item: item[0], reverse=True)
        limit = self.config.get('MEMORY_MAX_ITEMS', 6)
        budget = self.config.get('MEMORY_TOKEN_BUDGET', 1200)*2
        result = []
        for _, memory in scored:
            item = {'id': memory.id, 'memory_type': memory.memory_type, 'content': memory.content}
            size = len(encode(item))
            if size > budget:
                continue
            budget -= size
            result.append(item)
            memory.access_count += 1
            memory.last_accessed_at = now()
            with self.connect() as db:
                db.execute('UPDATE long_term_memories SET payload=? WHERE id=?', (memory.model_dump_json(), memory.id))
            if len(result) >= limit:
                break
        return result
