"""Trusted local decisions; MCP descriptions and annotations never grant permission."""
import os
import re
from dataclasses import dataclass
from langchain_core.messages import BaseMessage
from server.tool_registry import ToolUnavailable


class SecretFilter:
    def __init__(self, config=None):
        pairs = {**os.environ, **(config or {})}
        self.values = sorted({v for k, v in pairs.items() if isinstance(v, str) and len(v) >= 8
            and re.search(r'token|secret|password|api_key', k, re.I)}, key=len, reverse=True)

    def clean(self, value):
        if isinstance(value, BaseMessage):
            return value.model_copy(update={k: self.clean(v) for k, v in value.model_dump().items() if k != 'type'})
        if isinstance(value, str):
            for secret in self.values:
                value = value.replace(secret, '[REDACTED]')
            return re.sub(r'\b(?:gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{8,}|sk-[A-Za-z0-9_-]{12,})\b', '[REDACTED]', value)
        if isinstance(value, dict):
            return {k: '[REDACTED]' if re.search(r'^(?:authorization|password|api_key|access_token|token|secret)$', k, re.I)
                and isinstance(v, str) and v else self.clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.clean(v) for v in value)
        return value

    def require_safe(self, arguments):
        if self.clean(arguments) != arguments:
            raise ValueError('Secret-bearing tool arguments are prohibited')


# Local capability profiles describe permissions, never implement GitHub calls.
GITHUB_READS = {'get_me': 'github.context.read', 'get_repository': 'github.repo.read',
    'search_repositories': 'github.repo.search', 'get_file_contents': 'github.file.read',
    'search_code': 'github.code.search', 'list_issues': 'github.issue.list',
    'search_issues': 'github.issue.search', 'issue_read': 'github.issue.read',
    'get_issue': 'github.issue.read', 'get_issue_comments': 'github.issue.comments.read',
    'list_pull_requests': 'github.pr.list', 'search_pull_requests': 'github.pr.search',
    'pull_request_read': 'github.pr.read', 'get_pull_request': 'github.pr.read',
    'get_pull_request_files': 'github.pr.files.read', 'get_pull_request_diff': 'github.pr.diff.read'}
GITHUB_WRITES = {'issue_write': 'github.issue.create', 'create_issue': 'github.issue.create',
                 'add_issue_comment': 'github.issue.comment'}


def local_profile(server_id, name):
    if server_id == 'github':
        if name in GITHUB_READS:
            return GITHUB_READS[name], 'read', 'low', True
        if name in GITHUB_WRITES:
            return GITHUB_WRITES[name], 'write', 'high', True
        return 'github.unavailable.' + name, 'execute', 'critical', False
    # Unknown servers start with conservative execute/approval classification.
    return server_id + '.' + name, 'execute', 'high', True


@dataclass(frozen=True)
class PolicyDecision:
    requires_approval: bool
    reason: str


class ToolPolicy:
    def __init__(self, config):
        self.config = config
        self.secrets = SecretFilter(config)

    def evaluate(self, descriptor, arguments):
        self.secrets.require_safe(arguments)
        if not descriptor.enabled:
            raise ToolUnavailable('Tool disabled by local policy')
        operation, risk = descriptor.operation_type, descriptor.risk_level
        if descriptor.provider_type == 'mcp':
            _, operation, risk, enabled = local_profile(descriptor.provider, descriptor.metadata['remote_name'])
            if not enabled:
                raise ToolUnavailable('Tool is outside the local capability profile')
        if descriptor.provider == 'github':
            if operation != 'read':
                if self.config.get('GITHUB_MCP_READ_ONLY', True):
                    raise ToolUnavailable('GitHub write tools disabled by read-only mode')
                repository = f"{arguments.get('owner', '')}/{arguments.get('repo', '')}".casefold()
                allowed = {r.strip().casefold() for r in self.config.get('GITHUB_ALLOWED_REPOSITORIES', '').split(',') if r.strip()}
                if repository not in allowed:
                    raise PermissionError('Repository is outside the write allowlist')
                name = descriptor.metadata['remote_name']
                fields = {'owner', 'repo', 'title', 'body'} if name == 'create_issue' else (
                    {'owner', 'repo', 'title', 'body', 'method'} if name == 'issue_write' else {'owner', 'repo', 'issue_number', 'body'})
                if set(arguments) - fields or (name == 'issue_write' and arguments.get('method') != 'create'):
                    raise PermissionError('Only issue creation and comments are enabled')
        if operation == 'write' and not self.config.get('TOOL_APPROVE_WRITES', True):
            raise PermissionError('Write approvals disabled: writes are unavailable')
        if operation == 'delete' and not self.config.get('TOOL_APPROVE_DELETES', True):
            raise PermissionError('Delete approvals disabled: deletes are unavailable')
        required = operation in {'write', 'delete', 'execute'} or risk in {'high', 'critical'} or descriptor.requires_approval
        if descriptor.provider_type == 'native' and descriptor.name == 'native.analyze_data' and risk == 'medium':
            # Existing Phase 3 constrained runtime is authorized by the user's
            # analysis request; its AST/process restrictions remain mandatory.
            required = False
        required = required or (operation == 'read' and not self.config.get('TOOL_AUTO_APPROVE_READ', True))
        if required and not self.config.get('TOOL_APPROVAL_ENABLED', True):
            raise PermissionError('Approval disabled: sensitive tools fail closed')
        return PolicyDecision(required, f'{operation}/{risk}: human approval required' if required else 'Low-risk read permitted')
