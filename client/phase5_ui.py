"""Public supervisor/delegation progress; no private prompts or messages."""
import requests
import streamlit as st
from client.phase3_ui import request


def show_task_agents(api, task_id):
    try:
        runs = request(api, 'GET', f'/tasks/{task_id}/agents')['agents']
        if not runs:
            return
        st.write('Multi-Agent Task' if any(r['metadata'].get('orchestration_mode') == 'multi_agent' for r in runs) else 'Specialized Agent Task')
        supervisor=next((r for r in runs if r['agent_id']=='supervisor'),None)
        cost=supervisor['metadata'].get('cost_trace') if supervisor else None
        if cost:
            with st.expander('Orchestration efficiency'):
                columns=st.columns(3)
                columns[0].metric('LLM calls',cost['total_llm_calls'])
                columns[1].metric('Tool calls',cost['total_tool_calls'])
                columns[2].metric('Tokens',cost['tokens_total'] if cost['tokens_total'] is not None else 'unavailable')
                st.caption(f"Mode: {cost['orchestration_mode']} · Agents: {len({r['agent_id'] for r in runs if r['agent_id'] not in {'supervisor','reviewer'}})} · Cache hits: {cost['cache_hits']} · Reviewer: {cost['review_status']}")
                if cost.get('review_skip_reason'):st.caption('Review: '+cost['review_skip_reason'])
                if cost.get('budget'):
                    st.progress(min(1.0,cost['budget']['utilization']),text=f"Budget: {cost['budget']['utilization']:.0%}")
                decision=supervisor['metadata'].get('decision',{})
                st.caption('Why this mode? '+decision.get('reason_summary',''))
        for run in runs:
            icon = {'COMPLETED': '✓', 'RUNNING': '▶', 'FAILED': '×', 'PARTIAL': '◐'}.get(run['status'], '○')
            label = run['agent_id'].title() + (' Agent' if run['agent_id'] != 'supervisor' else '')
            with st.expander(f"{icon} {label} · {run['status']} · {run['id'][:8]}"):
                st.write(run['objective'])
                st.caption(f"Duration: {run['duration_ms']} ms · Tools: {run['tool_calls']} · LLM: {run['llm_calls']} · Iterations: {run['iterations']}")
                st.caption(f"Evidence: {run['metadata'].get('evidence_count', 0)} · Artifacts: {run['metadata'].get('artifact_count', 0)} · Tokens: {run['tokens'] if run['token_usage_status'] == 'available' else run['token_usage_status']}")
                if run['output_summary']:
                    st.write(run['output_summary'])
                if run['error_type']:
                    st.warning(run['error_type'])
    except (requests.RequestException, ValueError) as error:
        st.caption('Agent trace unavailable: ' + str(error))
