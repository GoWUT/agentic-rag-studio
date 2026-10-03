"""Identity boundary only; repository/service policy enforces authorization."""
import asyncio
import re
from uuid import uuid4
from fastapi import HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from server.auth.context import principal_context,request_id_context,LocalDevelopmentPrincipal

PUBLIC={'/','/health','/health/live','/health/ready','/auth/config',
        '/auth/register','/auth/login','/auth/refresh','/auth/logout','/metrics'}


class IdentityMiddleware(BaseHTTPMiddleware):
    def __init__(self,app,manager):
        super().__init__(app); self.manager=manager

    async def dispatch(self,request,call_next):
        supplied=request.headers.get('x-request-id','')
        request_id=supplied if re.fullmatch(r'[A-Za-z0-9_-]{1,64}',supplied) else str(uuid4())
        request_token=request_id_context.set(request_id); actor_token=None
        try:
            enabled=self.manager.config.get('AUTH_ENABLED',False)
            principal=LocalDevelopmentPrincipal()
            path=request.url.path
            if enabled and path not in PUBLIC and not (path in {'/docs','/openapi.json','/redoc'} and self.manager.config.get('API_DOCS_ENABLED',True)):
                header=request.headers.get('authorization','')
                scheme,_,token=header.partition(' ')
                if scheme.lower()!='bearer' or not token or len(token)>4096:
                    raise HTTPException(401,'Authentication required',headers={'WWW-Authenticate':'Bearer'})
                principal=await asyncio.to_thread(self.manager.auth.current_principal,token)
            request.state.principal=principal
            actor_token=principal_context.set(principal)
            if enabled and path.endswith('/refresh') and (path.startswith('/tools/') or path.startswith('/mcp/')):
                await asyncio.to_thread(self.manager.security.authorize,principal,'workspace.members.manage',request.query_params.get('workspace_id',''))
            if enabled and path=='/upload_pdf':
                raise HTTPException(409,'Create a workspace and upload its documents')
            if enabled and path.startswith('/indexes/'):
                file_id=path.rsplit('/',1)[-1]
                def check_index():
                    with self.manager.security.store.connect() as db:
                        rows=db.execute('SELECT workspace_id FROM workspace_documents WHERE fingerprint=?',(file_id,)).fetchall()
                        own=db.execute('SELECT session_id FROM sessions WHERE file_id=? AND created_by_user_id=?',(file_id,principal.user_id)).fetchall()
                    if not own and not any(self.manager.security.can(principal,'document.read',r['workspace_id']) for r in rows):
                        raise HTTPException(403,'Access denied')
                await asyncio.to_thread(check_index)
            response=await call_next(request)
        except HTTPException as error:
            response=JSONResponse(status_code=error.status_code,content={'detail':error.detail},headers=error.headers)
        except Exception as error:
            from sqlalchemy.exc import SQLAlchemyError
            from psycopg import Error
            from server.db.runtime import PersistenceUnavailable
            if not isinstance(error,(SQLAlchemyError,Error,PersistenceUnavailable)):
                raise
            response=JSONResponse(status_code=503,content={'detail':'Persistence unavailable'})
        finally:
            if actor_token is not None:principal_context.reset(actor_token)
            request_id_context.reset(request_token)
        response.headers.update({'X-Request-ID':request_id,'X-Content-Type-Options':'nosniff',
                                 'X-Frame-Options':'DENY','Referrer-Policy':'no-referrer'})
        return response


def register_security(app,manager):
    from server.auth.api import register_auth_api
    from starlette.middleware.cors import CORSMiddleware
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    register_auth_api(app,manager)
    from fastapi.exceptions import RequestValidationError
    @app.exception_handler(RequestValidationError)
    async def validation(request,error):
        return JSONResponse(status_code=422,content={'detail':[
            {'loc':list(e['loc']),'type':e['type'],'msg':e['msg']} for e in error.errors()]})
    app.add_middleware(IdentityMiddleware,manager=manager)
    hosts=[h.strip() for h in manager.config.get('TRUSTED_HOSTS','').split(',') if h.strip()]
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=hosts or ['*'])
    origins=[o.strip() for o in manager.config.get('CORS_ALLOWED_ORIGINS','').split(',') if o.strip()]
    app.add_middleware(CORSMiddleware,allow_origins=origins,allow_credentials=False,
                       allow_methods=['GET','POST','PATCH','DELETE'],allow_headers=['Authorization','Content-Type','X-Request-ID'])
