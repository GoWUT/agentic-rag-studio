"""Bounded metrics, private infrastructure spans and structured secret-safe logs."""
from contextlib import contextmanager
import json
import logging
import re
import time
from prometheus_client import CollectorRegistry,Counter,Histogram,Gauge,generate_latest
from server.tool_policy import SecretFilter
from server.auth.context import principal_context,request_id_context

COUNTERS=['http_requests_total','http_errors_total','auth_login_success_total','auth_login_failure_total',
    'auth_refresh_total','auth_denied_total','tasks_created_total','tasks_completed_total','tasks_failed_total',
    'worker_jobs_total','worker_job_failures_total','worker_retries_total','agent_runs_total',
    'multi_agent_activations_total','delegations_total','reviewer_runs_total','llm_calls_total','llm_tokens_total',
    'tool_calls_total','tool_failures_total','approvals_requested_total','approvals_approved_total','approvals_rejected_total']
HISTOGRAMS=['http_request_duration','task_duration','queue_wait_duration','agent_run_duration']
GAUGES=['queue_depth','worker_active_jobs']
LABELS={'method','route','status','agent','provider','capability','risk_level'}


class SecretRedactor(SecretFilter):
    def clean(self,value):
        if isinstance(value,dict):
            return {k:'[REDACTED]' if re.search(r'password|authorization|refresh.token|access.token|api.key|secret|database.url|email',k,re.I) else self.clean(v) for k,v in value.items()}
        value=super().clean(value)
        if isinstance(value,str):
            value=re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b','[REDACTED]',value)
            value=re.sub(r'(/[^\s\"]*)\?[^\s\"]+',r'\1?[REDACTED]',value)
            value=re.sub(r'(postgres(?:ql)?(?:\+psycopg)?://[^:\s/]+:)[^@\s]+@',r'\1[REDACTED]@',value)
            value=re.sub(r'(?i)Bearer\s+[A-Za-z0-9_.~-]+','Bearer [REDACTED]',value)
            value=re.sub(r'(?i)(password|refresh_token|access_token|api_key|secret)[\s\"\x27:=]+[^\s,;}]+',r'\1=[REDACTED]',value)
        return value


class SafeFormatter(logging.Formatter):
    def __init__(self,config):
        super().__init__('%(levelname)s %(name)s %(message)s'); self.config=config; self.redactor=SecretRedactor(config)
    def format(self,record):
        from opentelemetry import trace
        message=self.redactor.clean(record.getMessage())
        actor=principal_context.get(); span=trace.get_current_span().get_span_context()
        if self.config.get('LOG_FORMAT')!='json':return f'{record.levelname} {record.name} {message}'
        data={'level':record.levelname,'logger':record.name,'message':message,'request_id':request_id_context.get(),
              'trace_id':format(span.trace_id,'032x') if span.is_valid else None,'user_id':getattr(actor,'user_id',None)}
        for key in ('request_id','trace_id','user_id'):
            if hasattr(record,key):data[key]=getattr(record,key)
        for key in ('workspace_id','task_id','queue_job_id','agent_run_id','delegation_id','tool_execution_id','approval_id'):
            if hasattr(record,key):data[key]=getattr(record,key)
        if record.exc_info:data['error_type']=record.exc_info[0].__name__
        return json.dumps(self.redactor.clean(data),ensure_ascii=False)


class PrivacyExporter:
    """Drop bodies, SQL statements, connection strings and captured HTTP headers."""
    def __init__(self,exporter,config):self.exporter=exporter; self.redactor=SecretRedactor(config)
    def export(self,spans):
        from opentelemetry.sdk.trace import ReadableSpan
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.trace import Status
        allowed={'http.request.method','http.route','http.response.status_code','duration_ms','db.system','db.operation',
            'request_id','user_id','workspace_id','task_id','queue_job_id','agent_run_id','delegation_id','tool_execution_id',
            'approval_id','attempt','queue_wait_ms','status','tool_name','provider','capability','risk_level'}
        safe=[]
        for span in spans:
            resource=Resource({k:self.redactor.clean(v) for k,v in span.resource.attributes.items()
                               if k.startswith(('service.','telemetry.sdk.'))})
            safe.append(ReadableSpan(name=span.name,context=span.context,parent=span.parent,resource=resource,
                attributes={k:self.redactor.clean(v) for k,v in (span.attributes or {}).items() if k in allowed},
                events=(),links=(),kind=span.kind,status=Status(span.status.status_code),start_time=span.start_time,end_time=span.end_time,
                instrumentation_scope=span.instrumentation_scope))
        return self.exporter.export(safe)
    def shutdown(self):return self.exporter.shutdown()
    def force_flush(self,timeout_millis=30000):return self.exporter.force_flush(timeout_millis)


class Telemetry:
    def __init__(self,config):
        self.config=config; self.registry=CollectorRegistry(); self.metrics={}; self.tracer_provider=None; self.meter_provider=None
        # Fixed metric dimensions: no user/workspace/task/session/email/query labels.
        for name in COUNTERS:self.metrics[name]=Counter(name,name,registry=self.registry)
        for name in HISTOGRAMS:self.metrics[name]=Histogram(name,name,registry=self.registry)
        for name in GAUGES:self.metrics[name]=Gauge(name,name,registry=self.registry)
        self.http_count=Counter('http_route_requests','HTTP route requests',['method','route','status'],registry=self.registry)
        from opentelemetry import trace
        self.tracer=trace.get_tracer('agentic-rag-runtime')
        self.instruments={}
        if config.get('OTEL_ENABLED'):
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.metrics import MeterProvider
            from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
            from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
            resource=Resource.create({'service.name':config.get('OTEL_SERVICE_NAME','agentic-rag-api')})
            self.tracer_provider=TracerProvider(resource=resource)
            endpoint=config.get('OTEL_EXPORTER_OTLP_ENDPOINT','').rstrip('/')
            if endpoint:
                self.tracer_provider.add_span_processor(BatchSpanProcessor(PrivacyExporter(
                    OTLPSpanExporter(endpoint=endpoint+'/v1/traces',timeout=2),config),schedule_delay_millis=1000,
                    export_timeout_millis=3000,max_queue_size=512))
                reader=PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=endpoint+'/v1/metrics',timeout=2),
                    export_interval_millis=5000,export_timeout_millis=3000)
                self.meter_provider=MeterProvider(resource=resource,metric_readers=[reader])
                meter=self.meter_provider.get_meter('agentic-rag-runtime')
                for name in COUNTERS:self.instruments[name]=meter.create_counter(name)
                for name in HISTOGRAMS:self.instruments[name]=meter.create_histogram(name)
            self.tracer=self.tracer_provider.get_tracer('agentic-rag-runtime')

    def record(self,name,value=1):
        if not self.config.get('METRICS_ENABLED',True):return
        metric=self.metrics[name]
        if name in COUNTERS:metric.inc(value)
        elif name in HISTOGRAMS:metric.observe(value)
        else:metric.set(value)
        instrument=self.instruments.get(name)
        if instrument:
            if name in COUNTERS:instrument.add(value)
            else:instrument.record(value)

    @contextmanager
    def span(self,name,**attributes):
        with self.tracer.start_as_current_span(name,record_exception=False,set_status_on_exception=False) as span:
            actor=principal_context.get()
            attributes.setdefault('request_id',request_id_context.get())
            attributes.setdefault('user_id',getattr(actor,'user_id',None))
            for key,value in attributes.items():
                if value is not None:span.set_attribute(key,value)
            yield span

    def close(self):
        if self.meter_provider:self.meter_provider.shutdown(timeout_millis=3000)
        if self.tracer_provider:self.tracer_provider.shutdown()


def configure_telemetry(manager):
    if getattr(manager,'telemetry',None):return
    manager.telemetry=Telemetry(manager.config)
    if getattr(manager,'auth',None):manager.auth.store.telemetry=manager.telemetry
    formatter=SafeFormatter(manager.config)
    root=logging.getLogger()
    for name in ('server.http','server.worker'):logging.getLogger(name).setLevel(logging.INFO)
    if not root.handlers:root.addHandler(logging.StreamHandler())
    for logger in (root,logging.getLogger('uvicorn'),logging.getLogger('uvicorn.error'),logging.getLogger('uvicorn.access')):
        for handler in logger.handlers:handler.setFormatter(formatter)
    if manager.database and manager.config.get('OTEL_ENABLED'):
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        SQLAlchemyInstrumentor().instrument(engines=[manager.database.sync_engine,manager.database.async_engine.sync_engine],
            tracer_provider=manager.telemetry.tracer_provider,enable_commenter=False)


def register_observability(app,manager):
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.routing import Match
    from fastapi import Response
    configure_telemetry(manager)
    class HTTPMetrics(BaseHTTPMiddleware):
        async def dispatch(self,request,call_next):
            route=next((r.path for r in app.routes if hasattr(r,'path') and r.matches(request.scope)[0]==Match.FULL),'unknown')
            started=time.perf_counter()
            with manager.telemetry.span('http.request',**{'http.request.method':request.method,'http.route':route}) as span:
                response=await call_next(request)
                duration=time.perf_counter()-started
                span.set_attribute('http.response.status_code',response.status_code); span.set_attribute('duration_ms',duration*1000)
                span.set_attribute('request_id',response.headers.get('X-Request-ID',''))
                actor=getattr(request.state,'principal',None)
                if actor and getattr(actor,'user_id','local_default')!='local_default':span.set_attribute('user_id',actor.user_id)
                logging.getLogger('server.http').info('%s %s status=%s',request.method,route,response.status_code,
                    extra={'request_id':response.headers.get('X-Request-ID'),
                           'user_id':getattr(actor,'user_id',None),
                           **{k:request.path_params[k] for k in ('workspace_id','task_id') if k in request.path_params}})
                manager.telemetry.record('http_requests_total'); manager.telemetry.record('http_request_duration',duration)
                manager.telemetry.http_count.labels(request.method,route,str(response.status_code)).inc()
                if response.status_code>=400:manager.telemetry.record('http_errors_total')
                return response
    app.add_middleware(HTTPMetrics)
    @app.get('/metrics',include_in_schema=False)
    async def metrics():
        from fastapi import HTTPException
        if not manager.config.get('METRICS_ENABLED',True):raise HTTPException(404,'Metrics disabled')
        health=await manager.task_queue.health()
        manager.telemetry.record('queue_depth',health.get('queue_depth',0))
        return Response(generate_latest(manager.telemetry.registry),media_type='text/plain; version=0.0.4')


def worker_instrument(function):
    from functools import wraps
    @wraps(function)
    async def call(queue,context,task_id,expected_version):
        telemetry=getattr(queue.manager,'telemetry',None)
        if not telemetry:return await function(queue,context,task_id,expected_version)
        from sqlalchemy import select
        from server.db.schema import tasks
        from opentelemetry import context as otel_context,propagate
        async with queue.runtime.sessions() as session:
            payload=await session.scalar(select(tasks.c.payload).where(tasks.c.id==task_id))
        carrier=(payload or {}).get('metadata',{}).get('trace_context',{})
        token=otel_context.attach(propagate.extract(carrier)); started=time.perf_counter()
        request_token=request_id_context.set((payload or {}).get('metadata',{}).get('request_id'))
        telemetry.metrics['worker_active_jobs'].inc(); telemetry.record('worker_jobs_total')
        try:
            with telemetry.span('queue.task.execute',task_id=task_id,queue_job_id=context.job.id,
                                user_id=(payload or {}).get('created_by_user_id')) as span:
                result=await function(queue,context,task_id,expected_version)
                async with queue.runtime.sessions() as session:
                    final=await session.scalar(select(tasks.c.payload).where(tasks.c.id==task_id))
                if final:
                    span.set_attribute('status',final['status']); span.set_attribute('attempt',final.get('attempt_count',0))
                    wait=final.get('metadata',{}).get('queue_wait_ms',0); span.set_attribute('queue_wait_ms',wait)
                    telemetry.record('queue_wait_duration',wait/1000)
                    if (payload or {}).get('status') in {'QUEUED','RUNNING'}:
                        if final['status']=='COMPLETED':telemetry.record('tasks_completed_total')
                        elif final['status']=='FAILED':telemetry.record('tasks_failed_total')
                    logging.getLogger('server.worker').info('Task delivery ended status=%s',final['status'],extra={
                        'task_id':task_id,'queue_job_id':context.job.id,'workspace_id':final.get('workspace_id'),
                        'user_id':final.get('created_by_user_id')})
                return result
        except Exception as error:
            telemetry.record('worker_job_failures_total')
            from server.worker_runtime import RetryableTaskError
            if isinstance(error,RetryableTaskError):telemetry.record('worker_retries_total')
            async with queue.runtime.sessions() as session:
                final=await session.scalar(select(tasks.c.payload).where(tasks.c.id==task_id))
            if final and final['status']=='FAILED':telemetry.record('tasks_failed_total')
            raise
        finally:
            telemetry.metrics['worker_active_jobs'].dec(); telemetry.record('task_duration',time.perf_counter()-started)
            request_id_context.reset(request_token)
            otel_context.detach(token)
    return call


def task_span(function):
    from functools import wraps
    @wraps(function)
    def call(self,task_id,*args,**kwargs):
        telemetry=getattr(self.manager,'telemetry',None)
        if not telemetry:return function(self,task_id,*args,**kwargs)
        with telemetry.span('agent.task',task_id=task_id):return function(self,task_id,*args,**kwargs)
    return call


def tool_span(function):
    from functools import wraps
    @wraps(function)
    def call(self,registry,descriptor,arguments,state):
        telemetry=getattr(self.manager,'telemetry',None)
        if not telemetry:return function(self,registry,descriptor,arguments,state)
        started=time.perf_counter(); telemetry.record('tool_calls_total')
        with telemetry.span('tool.call',tool_name=descriptor.name,provider=descriptor.provider,
            capability=descriptor.capability,risk_level=descriptor.risk_level,task_id=state.get('task_id'),
            agent_run_id=state.get('agent_run_id'),delegation_id=state.get('delegation_id')) as span:
            try:
                result=function(self,registry,descriptor,arguments,state)
                span.set_attribute('status','SUCCEEDED' if result.success else 'FAILED')
                if not result.success:telemetry.record('tool_failures_total')
                for key in ('tool_execution_id','approval_id'):
                    if result.metadata.get(key):span.set_attribute(key,result.metadata[key])
                if result.metadata.get('approval_request_id'):span.set_attribute('approval_id',result.metadata['approval_request_id'])
                return result
            except Exception as error:
                from langgraph.errors import GraphInterrupt
                span.set_attribute('status','WAITING_USER' if isinstance(error,GraphInterrupt) else 'FAILED')
                if not isinstance(error,GraphInterrupt):telemetry.record('tool_failures_total')
                raise
            finally:span.set_attribute('duration_ms',(time.perf_counter()-started)*1000)
    return call


def agent_span(function):
    from functools import wraps
    @wraps(function)
    def call(self,state,config):
        telemetry=getattr(self.phase5.manager,'telemetry',None)
        if not telemetry:return function(self,state,config)
        request=state.get('delegation_request',{})
        agent=request.get('agent_id','reviewer')
        run_id=None
        if request.get('delegation_id'):
            run_id=self.phase5.store.delegation(request['delegation_id'])['agent_run_id']
        started=time.perf_counter()
        with telemetry.span('agent.'+agent,task_id=state.get('task_id') or request.get('task_id'),
            delegation_id=request.get('delegation_id'),agent_run_id=run_id,
            workspace_id=state.get('workspace_id') or request.get('context',{}).get('workspace_id')):
            try:return function(self,state,config)
            finally:telemetry.record('agent_run_duration',time.perf_counter()-started)
    return call
