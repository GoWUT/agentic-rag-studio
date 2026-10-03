"""Measured fixture outcomes; no unrun production accuracy claims."""
import json
from pathlib import Path


def evaluate():
    folder = Path(__file__).parent/'results'/'phase4'
    smoke = json.loads((folder/'runtime_smoke.json').read_text(encoding='utf-8'))
    checks = smoke['checks']
    def measured(name):
        return {'value': float(checks[name]['status'] == 'PASS'), 'denominator': 1,
            'source': 'real LLM + local official-SDK MCP synthetic smoke', 'check': name}
    metrics = {'Tool Selection Accuracy': measured('6_read_without_approval'),
        'Capability Resolution Accuracy': measured('5_dynamic_discovery'),
        'Approval Trigger Accuracy': measured('7_write_creates_approval'),
        'Tool Execution Success Rate': measured('10_approve_resumes'),
        'Failure Recovery Success Rate': measured('13_resume_after_restart'),
        'Unauthorized Write Rate': {'value': 0 if checks['9_reject_prevents_execution']['status'] == 'PASS' else None,
            'denominator': 1, 'check': 'rejected synthetic write'},
        'Duplicate Action Rate': {'value': 0 if checks['14_action_exactly_once']['status'] == 'PASS' else None,
            'denominator': 2, 'check': 'duplicate approve and resume both returned 409'}}
    report = {'metrics': metrics, 'limitations': 'Small synthetic fixtures; these are observed acceptance outcomes, not production benchmarks. No real GitHub action measured.'}
    (folder/'metrics.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    evaluate()
