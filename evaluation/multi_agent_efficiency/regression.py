"""Persist the actual unittest discovery result without inventing test counts."""
import json
from pathlib import Path
import unittest

ROOT=Path(__file__).resolve().parents[2]
def run():
    suite=unittest.defaultTestLoader.discover(str(ROOT),pattern='test*.py')
    result=unittest.TextTestRunner(verbosity=1).run(suite)
    value={'previous':373,'new':result.testsRun-373,'total':result.testsRun,
        'passed':result.testsRun-len(result.failures)-len(result.errors)-len(result.skipped),
        'failed':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
        'failures':[name.id() for name,_ in result.failures], 'error_tests':[name.id() for name,_ in result.errors]}
    (ROOT/'evaluation/results/phase5a5/tests.json').write_text(json.dumps(value,indent=2),encoding='utf-8')
    return result.wasSuccessful()

if __name__=='__main__':raise SystemExit(0 if run() else 1)
