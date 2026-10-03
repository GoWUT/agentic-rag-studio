"""Synthetic code-repository MCP fixture. This never connects to real GitHub."""
import argparse
import json
from pathlib import Path
from mcp.server import MCPServer

server = MCPServer('Phase 5A LOCAL MOCK repository')

@server.tool()
def get_file_contents(owner: str, repo: str, path: str = 'server/agent/graph.py') -> dict:
    """Read a synthetic repository fixture for code inspection."""
    return {'path':path,'sha':'synthetic-sha','html_url':f'https://github.com/{owner}/{repo}/blob/main/{path}',
        'content':'# SYNTHETIC CODE, NOT THE REAL REPOSITORY\ndef execute_plan(steps, max_steps=8):\n    results = []\n    for step in steps[:max_steps]:\n        results.append(run_tool(step))\n    return results\n# No retry, cancellation or policy gate is shown in this synthetic snippet.'}

@server.tool()
def create_issue(owner: str, repo: str, title: str, body: str = '') -> dict:
    """Append a local synthetic action after Policy/HITL; no real GitHub operation."""
    destination = Path(ledger)
    records = json.loads(destination.read_text()) if destination.exists() else []
    records.append({'owner':owner,'repo':repo,'title':title,'body':body})
    destination.write_text(json.dumps(records),encoding='utf-8')
    return {'number':len(records),'html_url':f'https://github.com/{owner}/{repo}/issues/{len(records)}'}

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--ledger',required=True)
    ledger = parser.parse_args().ledger
    server.run()
