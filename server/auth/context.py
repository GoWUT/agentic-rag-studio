from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    user_id: str
    email: str = ''
    status: str = 'ACTIVE'


@dataclass(frozen=True)
class LocalDevelopmentPrincipal:
    user_id: str = 'local_default'


@dataclass(frozen=True)
class SystemExecutionContext:
    actor: AuthenticatedPrincipal
    task_id: str


principal_context = ContextVar('authenticated_principal', default=None)
request_id_context = ContextVar('request_id', default=None)


@contextmanager
def principal_scope(principal):
    token = principal_context.set(principal)
    try:
        yield principal
    finally:
        principal_context.reset(token)


def require_principal(principal=None):
    from fastapi import HTTPException
    value = principal or principal_context.get()
    if not isinstance(value, AuthenticatedPrincipal):
        raise HTTPException(401, 'Authentication required', headers={'WWW-Authenticate':'Bearer'})
    return value


def principal_service(function):
    """Core services accept a principal or authenticated execution context."""
    from functools import wraps
    @wraps(function)
    def call(self,*args,**kwargs):
        supplied=kwargs.pop('principal',None)
        execution=kwargs.pop('execution_context',None)
        if execution is not None:
            from fastapi import HTTPException
            if not isinstance(execution,SystemExecutionContext) or not args or args[0]!=execution.task_id:
                raise HTTPException(403,'Invalid execution context')
            supplied=execution.actor
        manager=getattr(self,'manager',None) or (self if hasattr(self,'security') else None)
        if manager and getattr(manager,'security',None):
            principal=require_principal(supplied)
            with manager.security.as_principal(principal):return function(self,*args,**kwargs)
        return function(self,*args,**kwargs)
    return call
