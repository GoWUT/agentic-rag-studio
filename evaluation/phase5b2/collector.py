"""In-process OTLP HTTP protobuf receiver for acceptance, not production hosting."""
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
import threading
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest


class Collector:
    def __init__(self):
        self.spans=[]; self.metrics=[]; self.guard=threading.Lock(); collector=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                body=self.rfile.read(int(self.headers.get('Content-Length','0')))
                if self.path=='/v1/traces':
                    message=ExportTraceServiceRequest.FromString(body)
                    with collector.guard:
                        for resource in message.resource_spans:
                            for scope in resource.scope_spans:
                                for span in scope.spans:
                                    collector.spans.append({'name':span.name,'trace_id':span.trace_id.hex(),'span_id':span.span_id.hex(),
                                        'parent_span_id':span.parent_span_id.hex(),'attributes':{a.key:a.value.string_value or a.value.int_value for a in span.attributes},
                                        'events':len(span.events),'resource':{a.key:a.value.string_value for a in resource.resource.attributes}})
                elif self.path=='/v1/metrics':
                    message=ExportMetricsServiceRequest.FromString(body)
                    with collector.guard:
                        for resource in message.resource_metrics:
                            for scope in resource.scope_metrics:
                                for metric in scope.metrics:
                                    collector.metrics.append({'name':metric.name,'values':[p.as_int or p.as_double for p in metric.sum.data_points],
                                        'attributes':[{a.key:a.value.string_value for a in p.attributes} for p in metric.sum.data_points]})
                self.send_response(200); self.send_header('Content-Length','0'); self.end_headers()
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.port=self.server.server_port; self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
    def close(self):self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=3)
