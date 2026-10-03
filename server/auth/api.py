"""Public credential endpoints and protected live principal/membership endpoints."""
import asyncio
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from typing import Literal
from server.auth.context import require_principal


def get_current_principal(request:Request):
    return require_principal(getattr(request.state,'principal',None))


class RegisterRequest(BaseModel):
    email:str=Field(max_length=254)
    password:str=Field(max_length=128)
    display_name:str=Field(default='',max_length=200)


class LoginRequest(BaseModel):
    email:str=Field(max_length=254)
    password:str=Field(max_length=128)


class RefreshRequest(BaseModel):
    refresh_token:str=Field(min_length=1,max_length=512)


class PasswordRequest(BaseModel):
    old_password:str=Field(max_length=128)
    new_password:str=Field(max_length=128)


class MemberRequest(BaseModel):
    email:str=Field(max_length=254)
    role:Literal['OWNER','EDITOR','VIEWER']


class RoleRequest(BaseModel):
    role:Literal['OWNER','EDITOR','VIEWER']


def register_auth_api(app,manager):
    router=APIRouter()
    def auth():
        if not manager.config.get('AUTH_ENABLED'):raise HTTPException(503,'Authentication disabled in development')
        return manager.auth

    @router.get('/auth/config')
    def config():
        return {'enabled':manager.config.get('AUTH_ENABLED',False),
                'registration_enabled':manager.config.get('AUTH_ALLOW_REGISTRATION',True)}

    @router.post('/auth/register',status_code=201)
    def register(body:RegisterRequest):
        if not manager.config.get('AUTH_ALLOW_REGISTRATION',True):raise HTTPException(403,'Registration disabled')
        return auth().register(**body.model_dump())

    @router.post('/auth/login')
    def login(body:LoginRequest):return auth().login(**body.model_dump())

    @router.post('/auth/refresh')
    def refresh(body:RefreshRequest):return auth().refresh(body.refresh_token)

    @router.post('/auth/logout',status_code=204)
    def logout(body:RefreshRequest):auth().logout(body.refresh_token)

    @router.post('/auth/logout-all',status_code=204)
    def logout_all(principal=Depends(get_current_principal)):auth().logout_all(principal.user_id)

    @router.get('/auth/me')
    def me(principal=Depends(get_current_principal)):
        return auth().public(auth().store.principal_user(principal.user_id))

    @router.post('/auth/change-password',status_code=204)
    def password(body:PasswordRequest,principal=Depends(get_current_principal)):
        auth().change_password(principal.user_id,**body.model_dump())

    @router.get('/workspaces/{workspace_id}/members')
    def members(workspace_id:str,principal=Depends(get_current_principal)):
        return manager.security.members(workspace_id,principal)

    @router.get('/workspaces/{workspace_id}/role')
    def current_role(workspace_id:str,principal=Depends(get_current_principal)):
        return {'role':manager.security.authorize(principal,'workspace.read',workspace_id)}

    @router.post('/workspaces/{workspace_id}/members',status_code=201)
    def add_member(workspace_id:str,body:MemberRequest,principal=Depends(get_current_principal)):
        return manager.security.change_member(workspace_id,**body.model_dump(),add=True,principal=principal)

    @router.patch('/workspaces/{workspace_id}/members/{user_id}')
    def role(workspace_id:str,user_id:str,body:RoleRequest,principal=Depends(get_current_principal)):
        return manager.security.change_member(workspace_id,user_id=user_id,role=body.role,principal=principal)

    @router.delete('/workspaces/{workspace_id}/members/{user_id}',status_code=204)
    def remove(workspace_id:str,user_id:str,principal=Depends(get_current_principal)):
        manager.security.change_member(workspace_id,user_id=user_id,remove=True,principal=principal)
    app.include_router(router)
