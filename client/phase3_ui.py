"""Small sidebar panels and artifact previews for the existing Streamlit app."""
from io import BytesIO
import json
import pandas as pd
from client.auth_ui import api_requests as requests
from client.auth_ui import workspace_role
import streamlit as st


def request(api, method, path, **kwargs):
    response = requests.request(method,api+path,timeout=kwargs.pop('timeout',300),**kwargs)
    if not response.ok:
        try:
            detail = response.json().get('detail','Operation failed')
        except ValueError:
            detail = 'Operation failed'
        raise ValueError(str(detail))
    return response.json() if response.content else None


def show_artifacts(api, artifacts, prefix='artifact'):
    for artifact in artifacts:
        try:
            response = requests.get(api+'/artifacts/'+artifact['id'],timeout=10)
            response.raise_for_status()
            content = response.content
            mime = artifact['mime_type']
            st.caption(artifact['filename'])
            if mime == 'image/png':
                st.image(content)
            elif artifact['filename'].endswith('.csv'):
                st.dataframe(pd.read_csv(BytesIO(content)).head(100))
            elif mime == 'application/json':
                st.json(json.loads(content))
            else:
                st.text(content.decode('utf-8',errors='replace')[:2000])
            st.download_button('下载 '+artifact['filename'],content,file_name=artifact['filename'],mime=mime,key=prefix+'_'+artifact['id'])
        except (requests.RequestException,ValueError) as error:
            st.warning('Artifact 暂时不可用：'+str(error))


def sidebar(api,workspace_id,session_id,source_scope):
    role=workspace_role(api,workspace_id)
    with st.expander('Long-Term Memory'):
        try:
            scope = st.radio('Memory 范围',['User','Workspace'],horizontal=True,key='memory_scope')
            selected = workspace_id if scope == 'Workspace' else None
            if scope == 'Workspace' and not selected:
                st.info('请选择 Workspace。')
            else:
                memories = request(api,'GET','/memories',params={'workspace_id':selected} if selected else {})
                memories = [m for m in memories if m['scope_type']==('workspace' if selected else 'user')]
                with st.form('memory_create'):
                    content = st.text_area('记住的内容')
                    kind = st.selectbox('记忆类型',['preference','project','decision','instruction','episodic'])
                    submitted = st.form_submit_button('保存 Memory',disabled=bool(selected and role=='VIEWER'))
                if submitted:
                    request(api,'POST','/memories',json={'content':content,'memory_type':kind,'workspace_id':selected})
                    st.rerun()
                for memory in memories:
                    with st.form('memory_'+memory['id']):
                        edited = st.text_area(memory['memory_type'],memory['content'])
                        save = st.form_submit_button('编辑',disabled=bool(selected and role=='VIEWER'))
                        delete = st.form_submit_button('删除',disabled=bool(selected and role=='VIEWER'))
                    if save:
                        request(api,'PATCH','/memories/'+memory['id'],json={'content':edited})
                        st.rerun()
                    if delete:
                        request(api,'DELETE','/memories/'+memory['id'])
                        st.rerun()
        except (requests.RequestException,ValueError) as error:
            st.error('Memory 操作失败：'+str(error))
    if not workspace_id:
        return
    with st.expander('Datasets'):
        try:
            upload = st.file_uploader('Upload CSV / XLSX / JSON',type=['csv','xlsx','json'],key='dataset_upload')
            if st.button('上传数据集',disabled=upload is None or role=='VIEWER'):
                request(api,'POST',f'/workspaces/{workspace_id}/datasets',files={'file':(upload.name,upload,upload.type or 'application/octet-stream')})
                st.rerun()
            for asset in request(api,'GET',f'/workspaces/{workspace_id}/datasets'):
                st.caption(f"{asset['filename']} · {asset['row_count']} 行 · {asset['column_count']} 列 · {asset['status']}")
                if st.button('删除 '+asset['filename'],key='delete_dataset_'+asset['id'],disabled=role=='VIEWER'):
                    request(api,'DELETE',f"/workspaces/{workspace_id}/datasets/{asset['id']}")
                    st.rerun()
        except (requests.RequestException,ValueError) as error:
            st.error('Dataset 操作失败：'+str(error))
    with st.expander('Persistent Tasks'):
        try:
            with st.form('task_create'):
                goal = st.text_area('Task goal')
                submitted = st.form_submit_button('Run as Task（创建）')
            if submitted:
                request(api,'POST','/tasks',json={'workspace_id':workspace_id,'session_id':session_id,'goal':goal,'source_scope':source_scope})
                st.rerun()
            tasks = request(api,'GET','/tasks',params={'workspace_id':workspace_id})
            if tasks:
                labels = {t['id']:t['goal'][:40]+' · '+t['id'][:8]+' · '+t['status'] for t in tasks}
                selected = st.selectbox('Research Task',list(labels),format_func=lambda i:labels[i])
                task = request(api,'GET','/tasks/'+selected)
                st.write('Status: '+task['status'])
                from client.phase5_ui import show_task_agents
                show_task_agents(api, selected)
                pending = []
                if task['status'] == 'WAITING_USER':
                    pending = request(api,'GET','/approvals',params={'task_id':selected,'status':'PENDING'},timeout=2)['approvals']
                    for approval in pending:
                        st.info('等待人工审批：'+approval['capability']+' · '+approval['reason'])
                        st.caption('请在 Pending Approvals 面板查看并 Approve / Edit / Reject。')
                for step in task['steps']:
                    st.write({'COMPLETED':'✓','RUNNING':'▶'}.get(step['status'],'○')+' '+step['description']+' · '+step['status'])
                boundary = st.number_input('本次最多执行步骤（0 = 全部）',min_value=0,max_value=10,value=0)
                actions = {'PENDING':['run','cancel'],'QUEUED':['pause','cancel'],'RUNNING':['pause','cancel'],'PAUSED':['resume','cancel'],
                           'WAITING_USER':['resume','cancel'],'FAILED':['resume','cancel']}.get(task['status'],[])
                if pending:
                    actions = ['cancel']
                for action in actions:
                    can_control=role=='OWNER' or not st.session_state.get('_auth_enabled') or task.get('created_by_user_id')==st.session_state.get('_user',{}).get('id')
                    if st.button(action.title(),key='task_'+action,disabled=not can_control):
                        request(api,'POST',f'/tasks/{selected}/{action}',json={'max_steps':int(boundary) or None} if action in {'run','resume'} else {})
                        st.rerun()
                if st.button('刷新任务状态'):
                    st.rerun()
                if task.get('metadata',{}).get('answer'):
                    st.markdown(task['metadata']['answer'])
                show_artifacts(api,request(api,'GET',f'/tasks/{selected}/artifacts'),'task')
                if task.get('execution_backend') == 'worker' or task['status'] == 'QUEUED':
                    st.caption('后台 Worker 执行；关闭页面后任务继续。Pause 在安全边界生效。')
                    st.caption(f"Queue job: {task.get('queue_job_id')} · Attempt: {task.get('attempt_count',0)}")
                    if st.button('刷新后台状态', key='refresh_task_'+selected):
                        st.rerun()
                else:
                    st.caption('Inline 执行；Pause 在当前步骤完成后生效。')
        except (requests.RequestException,ValueError) as error:
            st.error('Task 操作失败：'+str(error))
