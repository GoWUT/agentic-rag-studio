"""Additive delegation ledger and per-agent bounded progress, on the existing task DB."""
import json
from server.persistence import Database, RecordNotFound, identity, now
from server.tasks import TaskConflict
from server.agent.orchestration_schemas import DelegationRequest, DelegationResult, SharedTaskContext, Finding


def merge_context(context, results, evidence, artifacts):
    context = SharedTaskContext.model_validate(context).model_copy(deep=True)
    known_evidence = {e['evidence_id'] for e in evidence}
    known_artifacts = {a['id'] for a in artifacts}
    existing = {r.delegation_id: r for r in context.delegation_results}
    for value in results:
        result = DelegationResult.model_validate(value)
        if set(result.evidence_ids) - known_evidence or set(result.artifact_ids) - known_artifacts:
            raise ValueError('Result references unknown evidence or artifacts')
        previous = existing.get(result.delegation_id)
        if previous and previous != result:
            raise ValueError('Conflicting delegation results')
        existing[result.delegation_id] = result
    context.delegation_results = list(existing.values())
    context.completed_delegations = [r.delegation_id for r in existing.values() if r.status == 'completed']
    context.evidence_ids = sorted({i for r in existing.values() for i in r.evidence_ids})
    context.artifact_ids = sorted({i for r in existing.values() for i in r.artifact_ids})
    context.known_facts = [Finding.model_validate(f) for r in existing.values() for f in r.payload.get('findings', [])
        if f.get('evidence_ids') and not set(f['evidence_ids']) - known_evidence]
    repaired = {r.revision_of for r in existing.values() if r.status == 'completed' and r.revision_of}
    context.open_questions = list(dict.fromkeys(q for r in existing.values() if r.delegation_id not in repaired for q in r.unresolved_questions))
    context.updated_at = now()
    return context


class AgentRunStore(Database):
    def __init__(self, path, secrets):
        super().__init__(path)
        self.secrets = secrets
        if self.backend == 'postgresql':
            return  # Application DDL is owned by Alembic.
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS agent_runs (id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), delegation_id TEXT UNIQUE, agent_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS agent_run_task ON agent_runs(task_id)')
            db.execute('CREATE INDEX IF NOT EXISTS agent_run_status ON agent_runs(status)')
            db.execute('CREATE TABLE IF NOT EXISTS agent_delegations (id TEXT PRIMARY KEY, task_id TEXT REFERENCES tasks(id), supervisor_run_id TEXT NOT NULL REFERENCES agent_runs(id), agent_id TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS delegation_task ON agent_delegations(task_id)')
            db.execute('CREATE INDEX IF NOT EXISTS delegation_status ON agent_delegations(status)')

    def _save_run(self, db, value):
        safe = self.secrets.clean(value)
        db.execute('UPDATE agent_runs SET status=?,payload=? WHERE id=?', (safe['status'], json.dumps(safe, ensure_ascii=False), safe['id']))

    @staticmethod
    def _get(db, table, run_id):
        row = db.execute('SELECT payload FROM ' + table + ' WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise RecordNotFound(run_id)
        return json.loads(row[0])

    def run(self, run_id):
        with self.connect() as db:
            return self._get(db, 'agent_runs', run_id)

    def delegation(self, delegation_id):
        with self.connect() as db:
            return self._get(db, 'agent_delegations', delegation_id)

    def supervisor(self, task_id):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM agent_runs WHERE task_id=? AND agent_id='supervisor' ORDER BY rowid DESC LIMIT 1", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def runs(self, task_id):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM agent_runs WHERE task_id=? ORDER BY rowid', (task_id,))]

    def delegations(self, supervisor_run_id=None, task_id=None):
        with self.connect() as db:
            values = [json.loads(row[0]) for row in db.execute('SELECT payload FROM agent_delegations ORDER BY rowid')]
        return [v for v in values if (not supervisor_run_id or v['supervisor_run_id'] == supervisor_run_id) and (not task_id or v['task_id'] == task_id)]

    def create_supervisor(self, task_id, goal, mode, metadata):
        self.secrets.require_safe({'goal': goal, 'metadata': metadata})
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if task_id:
                row = db.execute("SELECT payload FROM agent_runs WHERE task_id=? AND agent_id='supervisor' LIMIT 1", (task_id,)).fetchone()
                if row:
                    return json.loads(row[0])
            run = self._new_run(task_id, None, 'supervisor', goal)
            run['status'] = 'RUNNING'
            run['started_at'] = now()
            run['metadata'] = {'orchestration_mode': mode, **metadata}
            if getattr(self, 'efficiency_config', {}).get('MULTI_AGENT_EFFICIENCY_ENABLED', False):
                from server.agent.efficiency import BudgetManager
                run['metadata']['budget'] = BudgetManager.initial(self.efficiency_config)
            db.execute('INSERT INTO agent_runs VALUES (?,?,?,?,?,?)', (run['id'], task_id, None, 'supervisor', run['status'], json.dumps(run)))
        return run

    @staticmethod
    def _new_run(task_id, delegation_id, agent_id, objective):
        return {'id': identity(), 'task_id': task_id, 'delegation_id': delegation_id, 'agent_id': agent_id,
            'status': 'PENDING', 'objective': objective, 'input_summary': objective[:600], 'output_summary': '',
            'started_at': None, 'completed_at': None, 'duration_ms': None,
            'llm_calls': 0, 'tool_calls': 0, 'iterations': 0, 'tokens': None, 'token_usage_status': 'unavailable',
            'error_type': None, 'error_message': None, 'metadata': {}}

    def update_supervisor(self, run_id, **updates):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self._get(db, 'agent_runs', run_id)
            run['metadata'].update(updates)
            self._save_run(db, run)
        return run

    def create_delegation(self, request, maximum):
        request = DelegationRequest.model_validate(request)
        self.secrets.require_safe(request.model_dump())
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM agent_delegations WHERE id=?', (request.delegation_id,)).fetchone():
                return self._get(db, 'agent_delegations', request.delegation_id)
            count = db.execute('SELECT count(*) FROM agent_delegations WHERE supervisor_run_id=?', (request.supervisor_run_id,)).fetchone()[0]
            if count >= maximum:
                raise TaskConflict('Delegation budget exhausted')
            self._reserve_budget(db, self._get(db, 'agent_runs', request.supervisor_run_id), 'delegations',
                'reviewer' if request.agent_id=='reviewer' else 'supplementary' if request.revision_of else 'model')
            run = self._new_run(request.task_id, request.delegation_id, request.agent_id, request.objective)
            run['metadata'] = {'supervisor_run_id': request.supervisor_run_id, 'execution_group': request.execution_group, 'namespace': request.agent_id + ':' + request.delegation_id,
                'revision_of': request.revision_of, 'progress': {'cursor': 0, 'evidence': [], 'artifacts': [], 'outputs': [], 'actions': []}}
            value = {'id': request.delegation_id, 'task_id': request.task_id, 'supervisor_run_id': request.supervisor_run_id,
                'agent_run_id': run['id'], 'agent_id': request.agent_id, 'objective': request.objective, 'status': 'PENDING',
                'execution_group': request.execution_group, 'created_at': now(), 'started_at': None, 'completed_at': None,
                'request': request.model_dump(), 'result_json': None}
            db.execute('INSERT INTO agent_runs VALUES (?,?,?,?,?,?)', (run['id'], run['task_id'], run['delegation_id'], run['agent_id'], run['status'], json.dumps(run)))
            db.execute('INSERT INTO agent_delegations VALUES (?,?,?,?,?,?)', (value['id'], value['task_id'], value['supervisor_run_id'], value['agent_id'], value['status'], json.dumps(value)))
        return value

    def start(self, delegation_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            delegation = self._get(db, 'agent_delegations', delegation_id)
            run = self._get(db, 'agent_runs', delegation['agent_run_id'])
            if delegation['result_json'] is not None:
                return run
            run['status'] = delegation['status'] = 'RUNNING'
            run['started_at'] = run['started_at'] or now()
            delegation['started_at'] = run['started_at']
            run['metadata']['attempts'] = run['metadata'].get('attempts', 0) + 1
            if run['metadata']['attempts'] > 3:
                raise TaskConflict('Agent recovery attempts exhausted')
            self._save_run(db, run)
            db.execute('UPDATE agent_delegations SET status=?,payload=? WHERE id=?', (delegation['status'], json.dumps(delegation), delegation_id))
        return run

    def progress(self, run_id, **updates):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self._get(db, 'agent_runs', run_id)
            run['metadata']['progress'].update(updates)
            self._save_run(db, run)

    def count_call(self, run_id, kind, limit, usage=None):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self._get(db, 'agent_runs', run_id)
            if run[kind] >= limit:
                raise TaskConflict('Agent ' + kind + ' budget exhausted')
            if kind == 'tool_calls':
                self._reserve_budget(db, run, 'tool_calls')
                run = self._get(db, 'agent_runs', run_id)
            run[kind] += 1
            self._save_run(db, run)

    def add_usage(self, run_id, usage):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self._get(db, 'agent_runs', run_id)
            total = usage.get('total_tokens') if isinstance(usage, dict) else None
            if total is not None:
                run['tokens'] = (run['tokens'] or 0) + int(total)
                run['metadata']['usage_samples'] = run['metadata'].get('usage_samples', 0) + 1
            for field, source in [('tokens_prompt','input_tokens'),('tokens_completion','output_tokens')]:
                if isinstance(usage, dict) and usage.get(source) is not None:
                    run['metadata'][field] = run['metadata'].get(field, 0) + int(usage[source])
                    run['metadata'][field+'_samples'] = run['metadata'].get(field+'_samples', 0) + 1
            if run['metadata'].get('usage_samples', 0) == run['llm_calls']:
                run['token_usage_status'] = 'available'
            elif run['tokens'] is not None:
                run['token_usage_status'] = 'partial'
            self._save_run(db, run)
            supervisor = self._budget_owner(db, run)
            if supervisor and supervisor['metadata'].get('budget'):
                budget = supervisor['metadata']['budget']
                if total is not None:
                    budget['known_token_subtotal'] += int(total)
                    budget['token_samples'] += 1
                budget['tokens_used'] = budget['known_token_subtotal'] if budget['token_samples']==budget['llm_calls_used'] else None
                self._save_run(db, supervisor)

    def record_pre_orchestration_calls(self, run_id, analyzer_calls, router_calls):
        """Account for calls made before a supervisor existed; never reserve them twice."""
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self._get(db, 'agent_runs', run_id)
            phases = {k: v for k, v in {'query_analyzer': analyzer_calls, 'router': router_calls}.items() if v}
            run['llm_calls'] += sum(phases.values())
            run['metadata']['phase_calls'] = phases
            budget = run['metadata'].get('budget')
            if budget:
                budget['llm_calls_used'] += sum(phases.values())
                budget['phase_calls'] = dict(phases)
                if budget['llm_calls_used'] >= budget['limits']['max_llm_calls']:
                    budget['budget_exhausted'] = True
                    budget['exhausted_reasons'].append('llm_calls')
            self._save_run(db, run)

    def reserve_llm(self, run_id, call_limit, iteration_limit=None, phase='model'):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self._get(db, 'agent_runs', run_id)
            if run['llm_calls'] >= call_limit or (iteration_limit and run['iterations'] >= iteration_limit):
                raise TaskConflict('Agent model budget exhausted')
            self._reserve_budget(db, run, 'llm_calls', phase)
            # The reservation may also update this supervisor's own metadata.
            run = self._get(db, 'agent_runs', run_id)
            run['llm_calls'] += 1
            calls = run['metadata'].setdefault('phase_calls', {})
            calls[phase] = calls.get(phase, 0) + 1
            if iteration_limit:
                run['iterations'] += 1
            self._save_run(db, run)

    def _budget_owner(self, db, run):
        if run['agent_id'] == 'supervisor':
            return self._get(db, 'agent_runs', run['id'])
        supervisor_id = run['metadata'].get('supervisor_run_id')
        if not supervisor_id and run.get('delegation_id'):
            supervisor_id = self._get(db, 'agent_delegations', run['delegation_id'])['supervisor_run_id']
        return self._get(db, 'agent_runs', supervisor_id) if supervisor_id else None

    def _reserve_budget(self, db, run, kind, phase='model'):
        from server.agent.efficiency import BudgetManager, BudgetExhausted
        supervisor = self._budget_owner(db, run)
        if not supervisor or not supervisor['metadata'].get('budget'):
            return
        BudgetManager.resume(supervisor['metadata']['budget'])
        reason = BudgetManager.reserve(supervisor['metadata']['budget'], kind, phase)
        self._save_run(db, supervisor)
        if reason:
            # Preserve the denial/audit even though the attempted operation raises.
            db.commit()
            raise BudgetExhausted('Multi-agent budget exhausted: ' + reason)

    def pause_budget(self, run_id):
        from server.agent.efficiency import BudgetManager
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run=self._get(db,'agent_runs',run_id)
            if run['metadata'].get('budget'):
                BudgetManager.pause(run['metadata']['budget'])
                self._save_run(db,run)

    def annotate_run(self, run_id, **metadata):
        return self.update_supervisor(run_id, **metadata)

    def cached(self, task_id, supervisor_run_id, key):
        for d in self.delegations(task_id=task_id, supervisor_run_id=None if task_id else supervisor_run_id):
            if d['status']=='COMPLETED' and self.run(d['agent_run_id'])['metadata'].get('cache_key')==key:
                return d
        return None

    def finish(self, delegation_id, result, *, error_type=None):
        result = DelegationResult.model_validate(result)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            delegation = self._get(db, 'agent_delegations', delegation_id)
            if delegation['result_json'] is not None:
                return delegation['result_json']
            if result.delegation_id != delegation_id or result.agent_id != delegation['agent_id']:
                raise ValueError('Result identity mismatch')
            run = self._get(db, 'agent_runs', delegation['agent_run_id'])
            run['status'] = delegation['status'] = result.status.upper()
            run['completed_at'] = delegation['completed_at'] = now()
            from datetime import datetime
            run['duration_ms'] = round((datetime.fromisoformat(run['completed_at']) - datetime.fromisoformat(run['started_at'] or run['completed_at'])).total_seconds() * 1000, 2)
            run['output_summary'] = result.summary
            run['error_type'] = error_type
            run['error_message'] = error_type
            run['metadata'].update(evidence_count=len(result.evidence_ids), artifact_count=len(result.artifact_ids))
            delegation['result_json'] = self.secrets.clean(result.model_dump())
            self._save_run(db, run)
            db.execute('UPDATE agent_delegations SET status=?,payload=? WHERE id=?', (delegation['status'], json.dumps(delegation), delegation_id))
        return delegation['result_json']

    def recover(self):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT payload FROM agent_runs WHERE status='RUNNING'").fetchall():
                run = json.loads(row[0])
                run['status'] = 'INTERRUPTED'
                self._save_run(db, run)

    def cancel(self, task_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT payload FROM agent_delegations WHERE task_id=? AND status IN ('PENDING','RUNNING')", (task_id,)).fetchall():
                delegation = json.loads(row[0])
                delegation['status'] = 'CANCELLED'
                run = self._get(db, 'agent_runs', delegation['agent_run_id'])
                run['status'] = 'CANCELLED'
                self._save_run(db, run)
                db.execute('UPDATE agent_delegations SET status=?,payload=? WHERE id=?', ('CANCELLED', json.dumps(delegation), delegation['id']))
            for row in db.execute("SELECT payload FROM agent_runs WHERE task_id=? AND status IN ('PENDING','RUNNING','INTERRUPTED')", (task_id,)).fetchall():
                run = json.loads(row[0])
                run['status'] = 'CANCELLED'
                self._save_run(db, run)

    def claim_memory(self, run_id):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            run = self._get(db, 'agent_runs', run_id)
            if run['metadata'].get('memory_extraction_started'):
                return False
            run['metadata']['memory_extraction_started'] = True
            self._save_run(db, run)
            return True

    @staticmethod
    def public_run(run):
        return {k: run[k] for k in ('id', 'task_id', 'delegation_id', 'agent_id', 'status', 'objective', 'input_summary', 'output_summary',
            'started_at', 'completed_at', 'duration_ms', 'llm_calls', 'tool_calls', 'iterations', 'tokens', 'token_usage_status', 'error_type')} | {
            'metadata': {k: run['metadata'][k] for k in ('orchestration_mode', 'execution_group', 'namespace', 'revision_of', 'evidence_count', 'artifact_count',
                'memory_extraction_started', 'memory_extraction_completed', 'memory_written', 'cost_trace',
                'decision', 'review_status', 'review_skip_reason', 'cache_hit', 'cache_hits', 'duplicate_delegations_prevented',
                'budget', 'context_measurements', 'context_reduction', 'review_context_reduction', 'execution_group_metrics') if k in run['metadata']}}

    @staticmethod
    def public_delegation(value):
        return {k: v for k, v in value.items() if k != 'request'}
