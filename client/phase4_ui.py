"""Discovery and durable human decisions within the existing sidebar."""
import requests
import streamlit as st
from client.phase3_ui import request
from client.auth_ui import workspace_role


def sidebar(api):
    workspace=st.session_state.get('workspace_id')
    role=workspace_role(api,workspace)
    with st.expander('Tools / MCP'):
        try:
            providers = request(api, 'GET', '/tools/providers', timeout=1)['providers']
            for provider in providers:
                st.caption(provider['id'] + ': ' + provider['status'])
                if st.button('Refresh ' + provider['id'], key='refresh_provider_' + provider['id'],disabled=role!='OWNER'):
                    request(api, 'POST', '/tools/providers/' + provider['id'] + '/refresh',params={'workspace_id':workspace} if workspace else {})
                    st.rerun()
            for tool in request(api, 'GET', '/tools', timeout=1)['tools']:
                st.caption(f"{tool['capability']} · {tool['provider_type']} · {tool['risk_level']} · {'enabled' if tool['enabled'] else 'disabled'}")
        except (requests.RequestException, ValueError):
            st.caption('Tools unavailable')
    with st.expander('Pending Approvals'):
        try:
            approvals = request(api, 'GET', '/approvals', params={'status': 'PENDING'}, timeout=1)['approvals']
            if not approvals:
                st.caption('No pending approvals')
            for approval in approvals:
                aid = approval['id']
                args = approval['arguments']
                st.write(approval['provider'] + ' · ' + approval['capability'])
                st.caption('Task ' + approval['task_id'][:8] + ' · WAITING_USER · ' + approval['risk_level'])
                st.write('Repository: ' + args.get('owner', '') + '/' + args.get('repo', ''))
                st.caption(approval['reason'])
                st.json(args)
                title = st.text_input('Title', args.get('title', ''), key='approval_title_' + aid)
                body = st.text_area('Body', args.get('body', ''), key='approval_body_' + aid)
                for action in ('approve', 'edit', 'reject'):
                    if st.button(action.title(), key=action + '_' + aid):
                        payload = {'edits': {k: v for k, v in {'title': title, 'body': body}.items() if k in args}} if action == 'edit' else {}
                        request(api, 'POST', '/approvals/' + aid + '/' + action, json=payload)
                        st.rerun()
        except (requests.RequestException, ValueError) as error:
            st.warning('Approval operation failed: ' + str(error))
