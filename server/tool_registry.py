"""Provider-neutral discovery and immutable run definitions; no remote implementations."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from contextvars import ContextVar
from threading import RLock
from typing import Literal, Protocol

from jsonschema import validators
from pydantic import BaseModel, Field


class ToolUnavailable(ValueError):
    pass


class ToolTransportUnavailable(ToolUnavailable):
    """A transient transport failure; configuration/policy errors use the base class."""


class ToolDescriptor(BaseModel):
    name: str
    provider: str
    provider_type: Literal['native', 'mcp']
    description: str = ''
    input_schema: dict = Field(default_factory=lambda: {'type': 'object'})
    capability: str
    operation_type: Literal['read', 'write', 'delete', 'execute'] = 'execute'
    risk_level: Literal['low', 'medium', 'high', 'critical'] = 'high'
    requires_approval: bool = True
    enabled: bool = True
    metadata: dict = Field(default_factory=dict)


class ToolExecutionResult(BaseModel):
    success: bool
    content: list[dict] = Field(default_factory=list)
    structured_content: object = None
    error_type: str | None = None
    metadata: dict = Field(default_factory=dict)


class ToolProvider(Protocol):
    id: str
    async def list_tools(self) -> list[ToolDescriptor]: ...
    async def call_tool(self, name: str, arguments: dict) -> ToolExecutionResult: ...


def check_schema(schema):
    # Disallow remote refs: validation must never fetch attacker-selected resources.
    def visit(value):
        if isinstance(value, dict):
            for keyword in ('$ref', '$dynamicRef', '$recursiveRef'):
                if keyword in value and (not isinstance(value[keyword], str) or not value[keyword].startswith('#')):
                    raise ValueError('External schema references are disabled')
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(schema)
    validators.validator_for(schema).check_schema(schema)


def validate_arguments(tool, arguments):
    check_schema(tool.input_schema)
    validators.validator_for(tool.input_schema)(tool.input_schema).validate(arguments)


class ToolRegistry:
    def __init__(self, call_guard=None):
        self.providers = {}
        self.definitions = {}
        self.health = {}
        self.lock = RLock()
        self.call_guard = call_guard

    def register_provider(self, provider):
        with self.lock:
            if provider.id in self.providers:
                raise ValueError('Provider already registered')
            self.providers[provider.id] = provider
            self.health[provider.id] = 'not_connected'

    async def refresh(self, provider_id=None):
        ids = [provider_id] if provider_id else list(self.providers)
        for key in ids:
            provider = self.providers.get(key)
            if provider is None:
                raise ToolUnavailable('Unknown provider')
            try:
                tools = await provider.list_tools()
                proposed = {}
                for item in tools:
                    check_schema(item.input_schema)
                    if item.provider != key or item.name in proposed:
                        raise ValueError('Invalid or duplicate discovery name')
                    proposed[item.name] = item.model_dump()
                with self.lock:
                    foreign = {n: d for n, d in self.definitions.items() if d['provider'] != key}
                    if foreign.keys() & proposed.keys():
                        raise ValueError('Canonical name collision')
                    self.definitions = {**foreign, **proposed}
                    self.health[key] = 'connected'
            except Exception:
                with self.lock:
                    self.health[key] = 'unavailable'
        return self.list_tools()

    def snapshot(self):
        with self.lock:
            return deepcopy(self.definitions)

    def list_tools(self, snapshot=None, enabled_only=False):
        values = (snapshot if snapshot is not None else self.snapshot()).values()
        return [ToolDescriptor.model_validate(d) for d in values if not enabled_only or d['enabled']]

    def get_tool(self, name, snapshot=None):
        value = (snapshot if snapshot is not None else self.snapshot()).get(name)
        if not value or not value['enabled']:
            raise ToolUnavailable('Tool is unavailable or disabled')
        return ToolDescriptor.model_validate(value)

    def find_by_capability(self, capability, snapshot=None):
        result = [t for t in self.list_tools(snapshot, True) if t.capability == capability]
        if not result:
            raise ToolUnavailable('Capability unavailable: ' + capability)
        return sorted(result, key=lambda t: t.name)

    async def call_tool(self, name, arguments, *, snapshot=None, execution_id=None):
        descriptor = self.get_tool(name, snapshot)
        validate_arguments(descriptor, arguments)
        if self.call_guard:
            self.call_guard(descriptor, arguments, execution_id)
        provider = self.providers.get(descriptor.provider)
        if provider is None or self.health.get(descriptor.provider) != 'connected':
            raise ToolUnavailable('Provider unavailable')
        return await provider.call_tool(descriptor.metadata.get('remote_name', name), arguments)


NATIVE_CAPABILITIES = {'search_workspace': 'workspace.search', 'search_pdf': 'document.search',
                       'search_web': 'web.search', 'search_arxiv': 'arxiv.search',
                       'inspect_dataset': 'dataset.inspect', 'analyze_data': 'data.analyze'}


class NativeToolProvider:
    id = 'native'

    def __init__(self, tools):
        self.tools = {tool.name: tool for tool in tools}
        self.bindings = ContextVar('native_tool_bindings', default={})

    async def list_tools(self):
        return self.descriptors()

    def descriptors(self):
        return [ToolDescriptor(name='native.' + name, provider=self.id, provider_type='native',
            description=tool.description, input_schema=tool.args_schema.model_json_schema(),
            capability=NATIVE_CAPABILITIES.get(name, 'native.' + name), operation_type='execute' if name == 'analyze_data' else 'read',
            risk_level='medium' if name == 'analyze_data' else 'low', requires_approval=False, metadata={'remote_name': name})
            for name, tool in self.tools.items()]

    async def call_tool(self, name, arguments):
        if name not in self.tools:
            raise ToolUnavailable('Native tool unavailable')
        config = getattr(self, 'config_supplier', lambda: {})()
        implementation = self.bindings.get().get(name, self.tools[name])
        value = await asyncio.to_thread(implementation.invoke, arguments, config=config)
        return ToolExecutionResult(success=True, structured_content=value)
