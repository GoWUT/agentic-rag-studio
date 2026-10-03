"""Session-local tokens and one-refresh API client. No tokens in URLs/files/logs."""
import os
from urllib.parse import urlsplit
import requests as raw_requests
import streamlit as st

API_BASE=os.getenv('API_BASE','http://127.0.0.1:8001')


def clear_account_state():
    for key in list(st.session_state):del st.session_state[key]


class APIRequests:
    RequestException=raw_requests.RequestException
    Response=raw_requests.Response
    HTTPError=raw_requests.HTTPError

    def request(self,method,url,_named=False,**kwargs):
        local=(urlsplit(url).scheme,urlsplit(url).netloc)==(urlsplit(API_BASE).scheme,urlsplit(API_BASE).netloc)
        public=urlsplit(url).path.startswith('/auth/') and urlsplit(url).path not in {'/auth/me','/auth/logout-all','/auth/change-password'}
        kwargs=dict(kwargs); headers=dict(kwargs.pop('headers',{}))
        token=st.session_state.get('_access_token') if local and not public else None
        if token:headers['Authorization']='Bearer '+token
        positions=[]
        files=kwargs.get('files',{})
        for item in (files.values() if isinstance(files,dict) else [v for _,v in files]):
            stream=item[1] if isinstance(item,tuple) else item
            if hasattr(stream,'tell') and hasattr(stream,'seek'):positions.append((stream,stream.tell()))
        send=getattr(raw_requests,method.lower()) if _named else lambda target,**options:raw_requests.request(method,target,**options)
        response=send(url,headers=headers,**kwargs)
        refresh=st.session_state.get('_refresh_token')
        if response.status_code==401 and token and refresh:
            rotated=raw_requests.post(API_BASE+'/auth/refresh',json={'refresh_token':refresh},timeout=10)
            if rotated.ok:
                result=rotated.json(); st.session_state['_access_token']=result['access_token']; st.session_state['_refresh_token']=result['refresh_token']
                headers['Authorization']='Bearer '+result['access_token']
                for stream,position in positions:stream.seek(position)
                retried=send(url,headers=headers,**kwargs)
                if retried.status_code==401:clear_account_state()
                return retried
            clear_account_state()
        return response

    def get(self,url,**kwargs):return self.request('GET',url,_named=True,**kwargs)
    def post(self,url,**kwargs):return self.request('POST',url,_named=True,**kwargs)
    def patch(self,url,**kwargs):return self.request('PATCH',url,_named=True,**kwargs)
    def delete(self,url,**kwargs):return self.request('DELETE',url,_named=True,**kwargs)


api_requests=APIRequests()


def login_gate():
    from dotenv import dotenv_values
    enabled=os.getenv('AUTH_ENABLED',dotenv_values().get('AUTH_ENABLED','false'))
    if str(enabled).lower() not in {'true','1'}:
        st.session_state['_auth_enabled']=False
        st.sidebar.caption('本地开发模式：认证已关闭')
        return
    try:
        response=raw_requests.get(API_BASE+'/auth/config',timeout=3)
        response.raise_for_status(); config=response.json()
    except raw_requests.RequestException:
        st.error('API 暂时不可用。'); st.stop()
    if not config['enabled']:
        st.session_state['_auth_enabled']=False
        st.sidebar.caption('本地开发模式：认证已关闭')
        return
    st.session_state['_auth_enabled']=True
    if st.session_state.get('_access_token'):
        st.sidebar.caption('已登录：'+st.session_state.get('_user',{}).get('display_name',''))
        if st.sidebar.button('退出登录'):
            try:raw_requests.post(API_BASE+'/auth/logout',json={'refresh_token':st.session_state.get('_refresh_token','')},timeout=5)
            finally:clear_account_state()
            st.rerun()
        return
    st.title('Agentic RAG Studio')
    mode=st.radio('账户',['登录','注册'] if config['registration_enabled'] else ['登录'],horizontal=True)
    with st.form('account_form',clear_on_submit=True):
        email=st.text_input('Email',max_chars=254)
        display=st.text_input('显示名称',max_chars=200) if mode=='注册' else ''
        password=st.text_input('密码（12–128 字符）',type='password',max_chars=128)
        submitted=st.form_submit_button(mode)
    if submitted:
        try:
            if mode=='注册':
                response=raw_requests.post(API_BASE+'/auth/register',json={'email':email,'password':password,'display_name':display},timeout=10)
                if not response.ok:raise ValueError('账户无法创建，请检查输入。')
                st.success('账户已创建，请登录。')
            else:
                response=raw_requests.post(API_BASE+'/auth/login',json={'email':email,'password':password},timeout=10)
                if not response.ok:raise ValueError('Invalid credentials')
                tokens=response.json(); st.session_state['_access_token']=tokens['access_token']; st.session_state['_refresh_token']=tokens['refresh_token']
                me=api_requests.get(API_BASE+'/auth/me',timeout=5)
                if not me.ok:clear_account_state(); raise ValueError('账户暂时不可用。')
                st.session_state['_user']=me.json(); st.rerun()
        except (raw_requests.RequestException,ValueError) as error:st.error(str(error))
    st.stop()


def workspace_role(api,workspace_id):
    if not st.session_state.get('_auth_enabled'):return 'OWNER'
    if not workspace_id:return None
    response=api_requests.get(api+f'/workspaces/{workspace_id}/role',timeout=5)
    response.raise_for_status()
    return response.json()['role']


def membership_panel(api,workspace_id,role):
    st.caption('Workspace 角色：'+role)
    if role!='OWNER' or not st.session_state.get('_auth_enabled'):return
    with st.expander('Workspace 成员'):
        response=api_requests.get(api+f'/workspaces/{workspace_id}/members',timeout=5); response.raise_for_status()
        for member in response.json():
            st.caption(member['display_name']+' · '+member['email']+' · '+member['role'])
        with st.form('add_workspace_member'):
            email=st.text_input('已有账号 Email')
            selected_role=st.selectbox('新成员角色',['VIEWER','EDITOR','OWNER'])
            submitted=st.form_submit_button('添加成员')
        if submitted:
            result=api_requests.post(api+f'/workspaces/{workspace_id}/members',json={'email':email,'role':selected_role},timeout=5)
            if result.ok:st.rerun()
            st.error('添加成员失败。请确认账号已注册。')
