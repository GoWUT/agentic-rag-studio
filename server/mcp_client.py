"""MCP 2.x SDK adapter. Every operation owns a bounded SDK connection lifecycle."""
import asyncio
import os
import re
import json
from urllib.parse import urlsplit
import httpx2
from mcp import Client, StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel, ConfigDict, Field, model_validator

from server.persistence import Database, now
from server.tool_policy import SecretFilter, local_profile
from server.tool_registry import ToolDescriptor, ToolExecutionResult, ToolUnavailable, ToolTransportUnavailable, check_schema


def transient_transport_error(error):
    """SDK task groups can wrap timeouts; retry only known transport failures."""
    if isinstance(error, BaseExceptionGroup):
        return bool(error.exceptions) and all(transient_transport_error(e) for e in error.exceptions)
    return isinstance(error, (TimeoutError, ConnectionError, httpx2.TimeoutException,
                              httpx2.NetworkError, httpx2.RemoteProtocolError))


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(pattern=r'^[A-Za-z0-9_-]{1,64}$')
    name: str = ''
    transport: str = 'streamable_http'
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    enabled: bool = True
    connect_timeout_seconds: float = Field(default=10, gt=0, le=120)
    call_timeout_seconds: float = Field(default=30, gt=0, le=300)
    env_keys: list[str] = Field(default_factory=list)
    header_env_keys: dict[str, str] = Field(default_factory=dict)
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)

    @model_validator(mode='after')
    def valid(self):
        if self.transport not in {'stdio', 'streamable_http'}:
            raise ValueError('Unsupported MCP transport')
        if self.transport == 'stdio' and not self.command:
            raise ValueError('stdio command required')
        if self.transport == 'streamable_http':
            parsed = urlsplit(self.url or '')
            if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError('MCP endpoint must not contain credentials/query/fragment')
            if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}):
                raise ValueError('HTTPS endpoint or explicit loopback required')
            if self.id == 'github' and (parsed.hostname != 'api.githubcopilot.com' or not parsed.path.startswith('/mcp')):
                raise ValueError('GitHub profile requires the official MCP endpoint')
        if any(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key) for key in [*self.env_keys, *self.header_env_keys.values()]):
            raise ValueError('Only environment variable names may be stored')
        SecretFilter().require_safe({'id': self.id, 'name': self.name, 'command': self.command,
            'args': self.args, 'url': self.url, 'created_at': self.created_at, 'updated_at': self.updated_at,
            'environment_names': [*self.env_keys, *self.header_env_keys.keys(), *self.header_env_keys.values()]})
        return self


class MCPServerStore(Database):
    def __init__(self, path):
        super().__init__(path)
        if self.backend == 'postgresql':
            return  # Application DDL is owned by Alembic.
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS mcp_servers (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')

    def save(self, config):
        config = MCPServerConfig.model_validate(config)
        with self.connect() as db:
            db.execute('INSERT INTO mcp_servers VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload', (config.id, config.model_dump_json()))
        return config

    def list(self):
        with self.connect() as db:
            return [MCPServerConfig.model_validate_json(row[0]) for row in db.execute('SELECT payload FROM mcp_servers')]


class MCPToolProvider:
    def __init__(self, server, config, *, in_memory_server=None):
        self.server, self.config = server, config
        self.id = server.id
        self.in_memory_server = in_memory_server
        self.secrets = SecretFilter(config)
        self.connected = False
        self.last_error = None

    async def _request(self, operation, name=None, arguments=None):
        if not self.server.enabled:
            raise ToolUnavailable('MCP server disabled')
        timeout = self.server.connect_timeout_seconds + self.server.call_timeout_seconds
        try:
            async with asyncio.timeout(timeout):
                if self.in_memory_server is not None:
                    async with Client(self.in_memory_server, read_timeout_seconds=self.server.connect_timeout_seconds) as client:
                        return await self._operation(client, operation, name, arguments)
                if self.server.transport == 'stdio':
                    env = {key: os.environ[key] for key in self.server.env_keys if key in os.environ}
                    if self.id == 'github':
                        env.update(GITHUB_TOOLSETS=self.config.get('MCP_GITHUB_TOOLSETS', 'context,repos,issues,pull_requests'),
                            GITHUB_READ_ONLY='1' if self.config.get('GITHUB_MCP_READ_ONLY', True) else '0',
                            GITHUB_LOCKDOWN_MODE='1' if self.config.get('GITHUB_MCP_LOCKDOWN', True) else '0')
                        if self.config.get('MCP_GITHUB_TOOL_ALLOWLIST'):
                            env['GITHUB_TOOLS'] = self.config['MCP_GITHUB_TOOL_ALLOWLIST']
                    # Never forward stderr from a remote process into application logs.
                    with open(os.devnull, 'w') as sink:
                        params = StdioServerParameters(command=self.server.command, args=self.server.args, env=env)
                        async with Client(stdio_client(params, errlog=sink), read_timeout_seconds=self.server.connect_timeout_seconds) as client:
                            return await self._operation(client, operation, name, arguments)
                headers = {header: os.environ[key] for header, key in self.server.header_env_keys.items() if key in os.environ}
                if self.id == 'github':
                    token = os.environ.get('GITHUB_PERSONAL_ACCESS_TOKEN', '')
                    if not token:
                        raise ToolUnavailable('GitHub credential not configured')
                    headers.update({'Authorization': 'Bearer ' + token,
                        'X-MCP-Toolsets': self.config.get('MCP_GITHUB_TOOLSETS', 'context,repos,issues,pull_requests'),
                        'X-MCP-Readonly': 'true' if self.config.get('GITHUB_MCP_READ_ONLY', True) else 'false',
                        'X-MCP-Lockdown': 'true' if self.config.get('GITHUB_MCP_LOCKDOWN', True) else 'false'})
                    if self.config.get('MCP_GITHUB_TOOL_ALLOWLIST'):
                        headers['X-MCP-Tools'] = self.config['MCP_GITHUB_TOOL_ALLOWLIST']
                async with httpx2.AsyncClient(headers=headers, trust_env=False,
                    timeout=httpx2.Timeout(self.server.connect_timeout_seconds, read=self.server.call_timeout_seconds)) as http:
                    async with Client(streamable_http_client(self.server.url, http_client=http), read_timeout_seconds=self.server.connect_timeout_seconds) as client:
                        return await self._operation(client, operation, name, arguments)
        except Exception as error:
            self.connected, self.last_error = False, type(error).__name__
            if transient_transport_error(error):
                raise ToolTransportUnavailable('MCP transport temporarily unavailable') from None
            raise ToolUnavailable('MCP request failed: ' + type(error).__name__) from None

    async def _operation(self, client, operation, name, arguments):
        self.connected, self.last_error = True, None
        if operation == 'list':
            tools, cursor = [], None
            for _ in range(100):
                result = await client.list_tools(cursor=cursor, cache_mode='refresh')
                tools.extend(result.tools)
                cursor = getattr(result, 'next_cursor', None)
                if not cursor:
                    return tools
            raise ValueError('Discovery pagination limit exceeded')
        return await client.call_tool(name, arguments, read_timeout_seconds=self.server.call_timeout_seconds)

    async def list_tools(self):
        raw = await self._request('list')
        tools = []
        allowlist = {n.strip() for n in self.config.get('MCP_GITHUB_TOOL_ALLOWLIST', '').split(',') if n.strip()}
        for item in raw:
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', item.name):
                continue
            value = item.model_dump(by_alias=True, mode='json')
            schema = value.get('inputSchema', value.get('input_schema', {}))
            try:
                check_schema(schema)
            except Exception:
                continue
            capability, operation, risk, enabled = local_profile(self.id, item.name)
            if self.id == 'github':
                enabled = enabled and (not allowlist or item.name in allowlist)
                enabled = enabled and not (operation != 'read' and self.config.get('GITHUB_MCP_READ_ONLY', True))
            tools.append(ToolDescriptor(name=f'mcp.{self.id}.{item.name}', provider=self.id, provider_type='mcp',
                description=self.secrets.clean(item.description or '')[:2000], input_schema=self.secrets.clean(schema),
                capability=capability, operation_type=operation, risk_level=risk, requires_approval=operation != 'read',
                enabled=enabled, metadata={'remote_name': item.name, 'raw_input_schema': self.secrets.clean(schema),
                    'annotations': self.secrets.clean(value.get('annotations')), 'untrusted': True}))
        return tools

    async def call_tool(self, name, arguments):
        raw = await self._request('call', name, arguments)
        value = self.secrets.clean(raw.model_dump(by_alias=True, mode='json'))
        content = value.get('content', [])
        # Drop binary content from the graph; retain only bounded text/resource metadata.
        normalized = [{k: v for k, v in block.items() if k != 'data'} for block in content]
        structured = value.get('structuredContent', value.get('structured_content'))
        if structured is None:
            try:
                structured = json.loads(next((b.get('text', '') for b in normalized if b.get('type') == 'text'), 'null'))
            except (ValueError, TypeError):
                pass
        # The protocol can return large data; bound material entering model/state.
        for block in normalized:
            if isinstance(block.get('text'), str):
                block['text'] = block['text'][:32000]
        if len(json.dumps(structured, ensure_ascii=False)) > 32000:
            structured = {'truncated': True, 'text': json.dumps(structured, ensure_ascii=False)[:32000]}
        failure = value.get('isError', value.get('is_error', False))
        return ToolExecutionResult(success=not failure, content=normalized, structured_content=structured,
            error_type='MCPToolError' if failure else None, metadata={'server_id': self.id, 'untrusted': True})

    async def connect(self):
        return await self.list_tools()

    async def disconnect(self):
        self.connected = False

    async def reconnect(self):
        await self.disconnect()
        return await self.connect()

    def health(self):
        return {'connected': self.connected, 'last_error': self.last_error}
