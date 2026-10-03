"""Operator-owned identities and capability routing; workers cannot mutate this registry."""
from threading import RLock
from server.agent.orchestration_schemas import AgentDescriptor, AgentRegistrySnapshot, DelegationRequest


class AgentUnavailable(ValueError):
    pass


class CapabilityUnavailable(ValueError):
    pass


class DelegationRejected(ValueError):
    pass


class AgentRegistry:
    def __init__(self):
        self._agents = {}
        self._lock = RLock()

    def register(self, descriptor):
        descriptor = AgentDescriptor.model_validate(descriptor)
        if descriptor.can_delegate and descriptor.agent_id != 'supervisor':
            raise DelegationRejected('Only the supervisor may delegate')
        with self._lock:
            if descriptor.agent_id in self._agents:
                raise ValueError('Agent already registered')
            self._agents[descriptor.agent_id] = descriptor.model_dump()

    def snapshot(self):
        with self._lock:
            return AgentRegistrySnapshot(agents=self._agents).model_copy(deep=True)

    def list_agents(self, snapshot=None):
        return list((snapshot or self.snapshot()).agents.values())

    def get(self, agent_id, snapshot=None):
        agent = (snapshot or self.snapshot()).agents.get(agent_id)
        if agent is None or not agent.enabled:
            raise AgentUnavailable('Agent unavailable: ' + agent_id)
        return agent.model_copy(deep=True)

    def find_by_capability(self, capability, snapshot=None):
        agents = [a for a in self.list_agents(snapshot) if a.enabled and capability in a.capabilities]
        if not agents:
            raise CapabilityUnavailable('Agent capability unavailable: ' + capability)
        return sorted(agents, key=lambda a: a.agent_id)

    def enable(self, agent_id):
        self._set_enabled(agent_id, True)

    def disable(self, agent_id):
        self._set_enabled(agent_id, False)

    def _set_enabled(self, agent_id, enabled):
        with self._lock:
            if agent_id not in self._agents:
                raise AgentUnavailable(agent_id)
            self._agents[agent_id] = {**self._agents[agent_id], 'enabled': enabled}

    def validate(self, request, snapshot, *, config, cancelled=False, delegation_count=0):
        request = DelegationRequest.model_validate(request)
        agent = self.get(request.agent_id, snapshot)
        if cancelled or request.agent_id == 'supervisor':
            raise DelegationRejected('Task cancelled or recursive delegation')
        if not request.required_capabilities or set(request.required_capabilities) - set(agent.capabilities):
            raise CapabilityUnavailable('Agent capability mismatch')
        if set(request.allowed_tool_capabilities) - set(agent.tool_capabilities):
            raise DelegationRejected('Tool permission violation')
        if request.max_iterations > agent.max_iterations or request.max_tool_calls > agent.max_tool_calls:
            raise DelegationRejected('Agent budget exceeded')
        if request.depth > min(1, config.get('MULTI_AGENT_MAX_DEPTH', 1)):
            raise DelegationRejected('Delegation depth exceeded')
        if delegation_count >= config.get('MULTI_AGENT_MAX_DELEGATIONS', 8):
            raise DelegationRejected('Delegation count exceeded')
        if request.expected_output != agent.output_contract:
            raise DelegationRejected('Output contract mismatch')
        return request


def default_registry(config):
    registry = AgentRegistry()
    definitions = [
        ('supervisor', 'Supervisor', ['orchestrate.route', 'orchestrate.delegate', 'orchestrate.synthesize'], [], 'SupervisorPlan'),
        ('research', 'Research Agent', ['research.document', 'research.web', 'research.academic', 'research.github', 'research.compare'],
         ['workspace.search', 'document.search', 'web.search', 'arxiv.search', 'github.repo.read', 'github.file.read', 'github.code.search', 'github.issue.read', 'github.pr.read', 'github.issue.list', 'github.pr.list'], 'ResearchAgentResult'),
        ('data', 'Data Agent', ['data.inspect', 'data.analyze', 'data.visualize', 'data.compare'],
         ['dataset.inspect', 'data.analyze'], 'DataAgentResult'),
        ('coding', 'Coding Agent', ['code.inspect', 'code.explain', 'code.review', 'code.propose_patch'],
         ['github.repo.read', 'github.file.read', 'github.code.search'], 'CodingAgentResult'),
        ('reviewer', 'Reviewer Agent', ['review.evidence', 'review.completeness', 'review.consistency', 'review.requirements'],
         ['evidence.read', 'artifact.read'], 'ReviewerResult'),
    ]
    for agent_id, name, caps, tools, contract in definitions:
        prefix = agent_id.upper() + '_AGENT'
        registry.register(AgentDescriptor(agent_id=agent_id, name=name, description=name + ' with bounded structured results',
            capabilities=caps, tool_capabilities=tools, can_delegate=agent_id == 'supervisor',
            max_iterations=config.get(prefix + '_MAX_ITERATIONS', config.get('MULTI_AGENT_SUPERVISOR_MAX_LLM_CALLS',10) if agent_id=='supervisor' else 4 if agent_id != 'reviewer' else 2),
            max_tool_calls=config.get(prefix + '_MAX_TOOL_CALLS', 4 if agent_id != 'reviewer' else 0), output_contract=contract))
    return registry
