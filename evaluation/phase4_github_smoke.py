"""Optional real GitHub READ-only smoke. Never calls a write tool."""
import asyncio
import json
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from server.mcp_client import MCPServerConfig, MCPToolProvider
from server.tool_registry import ToolRegistry
from server.tool_policy import ToolPolicy


async def evaluate():
    load_dotenv(ROOT/'.env', override=False)
    output = ROOT/'evaluation/results/phase4/github_smoke.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    result = {'source': 'REAL GitHub official MCP, no fixture substitution', 'checks': {},
        'write': {'status': 'SKIPPED', 'reason': 'This runner only reads; write smoke requires explicit test repo/flag and durable human approval'}}
    checks = ['17_github_connect', '18_github_discovery', '19_repository_read', '20_issue_pr_read']
    if not os.environ.get('GITHUB_PERSONAL_ACCESS_TOKEN'):
        result['checks'] = {key: {'status': 'SKIPPED', 'reason': 'GITHUB_PERSONAL_ACCESS_TOKEN absent'} for key in checks}
    else:
        config = {'GITHUB_MCP_READ_ONLY': True, 'GITHUB_MCP_LOCKDOWN': True,
            'MCP_GITHUB_TOOLSETS': 'context,repos,issues,pull_requests'}
        provider = MCPToolProvider(MCPServerConfig(id='github', url='https://api.githubcopilot.com/mcp/'), config)
        registry = ToolRegistry()
        registry.register_provider(provider)
        await registry.refresh()
        connected = registry.health['github'] == 'connected'
        result['checks'][checks[0]] = {'status': 'PASS' if connected else 'FAIL'}
        result['checks'][checks[1]] = {'status': 'PASS' if registry.list_tools(enabled_only=True) else 'FAIL'}
        if connected:
            repository = os.environ.get('PHASE4_GITHUB_READ_REPOSITORY', 'GoWUT/agentic-rag-studio')
            owner, repo = repository.split('/', 1)
            for key, capability, arguments in [(checks[2], 'github.file.read', {'owner': owner, 'repo': repo, 'path': 'README.md'}),
                (checks[3], 'github.issue.list', {'owner': owner, 'repo': repo, 'perPage': 1})]:
                try:
                    tool = registry.find_by_capability(capability)[0]
                    ToolPolicy(config).evaluate(tool, arguments)
                    response = await registry.call_tool(tool.name, arguments)
                    result['checks'][key] = {'status': 'PASS' if response.success else 'FAIL', 'repository': repository, 'tool': tool.name}
                except Exception as error:
                    result['checks'][key] = {'status': 'FAIL', 'error_type': type(error).__name__}
        else:
            for key in checks[2:]:
                result['checks'][key] = {'status': 'SKIPPED', 'reason': 'Connection failed'}
    output.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    asyncio.run(evaluate())
