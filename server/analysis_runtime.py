"""Constrained subprocess data analysis. This is NOT an OS security boundary."""
import ast
import hashlib
import json
import logging
import mimetypes
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Thread
import time
import os

from pydantic import BaseModel, Field
from server.data_assets import Artifact
from server.persistence import identity

LOGGER = logging.getLogger(__name__)
IMPORTS = {'pandas','numpy','matplotlib.pyplot','statistics','math','json','csv','re','datetime','collections'}
FORBIDDEN = {'eval','exec','compile','__import__','globals','locals','getattr','setattr','delattr','vars','dir','open','input','breakpoint','help','exit','quit','type','object','memoryview'}
IO_METHODS = {'load','loads','dump','dumps','fromfile','tofile','loadtxt','genfromtxt','save','savez','savez_compressed','memmap','imread','imsave','read','write','read_text','write_text','open','read_pickle','to_pickle','to_excel','to_sql','read_sql','connect','system','popen'}
MODULE_CALLS = {
    'pandas': {'DataFrame','Series','concat','merge','to_numeric','to_datetime','to_timedelta','crosstab','pivot_table','isna','notna','Timestamp','date_range','cut','qcut'},
    'numpy': {'array','asarray','mean','median','std','var','sum','min','max','corrcoef','sqrt','abs','where','isnan','nan','inf','arange','linspace','zeros','ones','round','log','exp','percentile','quantile','unique','sort','argsort'},
    'matplotlib.pyplot': {'figure','subplots','bar','barh','plot','scatter','hist','boxplot','pie','title','xlabel','ylabel','legend','xticks','yticks','grid','tight_layout','savefig','close','axhline','axvline','gcf','text','annotate','xlim','ylim','tick_params','colorbar'},
    'json': {'dumps','loads'}, 'csv': set(),
    'collections': {'Counter','defaultdict','deque','OrderedDict','ChainMap','namedtuple'},
    'datetime': {'datetime','date','time','timedelta','timezone'},
    'statistics': {'mean','median','median_low','median_high','mode','multimode','stdev','pstdev','variance','pvariance','harmonic_mean','geometric_mean','quantiles','NormalDist'},
    'math': {'sqrt','log','log2','log10','exp','pow','sin','cos','tan','ceil','floor','isfinite','isnan','isclose','fabs','fsum','prod','comb','pi','e','inf','nan'},
    're': {'compile','search','match','fullmatch','findall','finditer','sub','split','escape','IGNORECASE','MULTILINE','DOTALL'},
}


class UnsafeCode(ValueError):
    pass


class DataAnalysisRequest(BaseModel):
    dataset_ids: list[str] = Field(min_length=1, max_length=10)
    objective: str = Field(min_length=1, max_length=3000)
    code: str = ''


class GeneratedCode(BaseModel):
    code: str


class CodeExecutionResult(BaseModel):
    execution_id: str
    workspace_id: str
    task_id: str | None = None
    status: str
    objective: str
    stdout: str = ''
    stderr: str = ''
    duration_ms: float = 0
    dataset_ids: list[str]
    artifacts: list[dict] = Field(default_factory=list)
    code_hash: str
    error_type: str | None = None


def validate_code(code, max_chars=12000):
    if len(code) > max_chars:
        raise UnsafeCode('Code size limit exceeded')
    tree = ast.parse(code)
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import,ast.ImportFrom)):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name not in IMPORTS:
                        raise UnsafeCode('Forbidden import')
                    aliases[alias.asname or alias.name.split('.')[0]] = alias.name
            else:
                if node.module not in IMPORTS or any(a.name.startswith('_') or a.name == '*' for a in node.names):
                    raise UnsafeCode('Forbidden import')
                allowed = MODULE_CALLS.get(node.module)
                if allowed is not None and any(a.name not in allowed for a in node.names):
                    raise UnsafeCode('Forbidden imported function')
        if isinstance(node, ast.Name) and (node.id.startswith('__') or node.id in FORBIDDEN):
            raise UnsafeCode('Forbidden name')
        if isinstance(node, ast.Attribute):
            if node.attr.startswith('_') or node.attr in {'io','api','compat','core','testing','ctypes','builtins','importlib','query','eval','sys','os','modules','globals','locals','env','style','canvas','manager'} or node.attr.startswith('read_') or node.attr.startswith('print_') or node.attr in (IO_METHODS-{'loads','dumps'}):
                raise UnsafeCode('Forbidden attribute or file access')
            if isinstance(node.value,ast.Name) and node.value.id in aliases:
                allowed = MODULE_CALLS.get(aliases[node.value.id],set())
                if node.attr not in allowed:
                    raise UnsafeCode('Module attribute not allowed')
            if node.attr.startswith('to_') and node.attr not in {'to_csv','to_json','to_numpy','to_dict','to_list','to_string','to_frame','to_numeric','to_datetime','to_timedelta'}:
                raise UnsafeCode('Unsupported serializer')
        if isinstance(node, ast.Constant) and isinstance(node.value,str) and re_path(node.value):
            raise UnsafeCode('Host paths and network URLs are forbidden')
        if isinstance(node,(ast.ClassDef,ast.AsyncFunctionDef,ast.Await,ast.Global,ast.Nonlocal)):
            raise UnsafeCode('Unsupported executable structure')
    for node in ast.walk(tree):
        if not isinstance(node,ast.Call):
            continue
        if isinstance(node.func,ast.Name) and node.func.id == 'write_artifact':
            target = node.args[0] if node.args else None
            if not isinstance(target,ast.Constant) or not isinstance(target.value,str) or '/' in target.value or '\\' in target.value or ':' in target.value or target.value.startswith('.') or Path(target.value).suffix.lower() not in {'.json','.txt','.csv'}:
                raise UnsafeCode('Invalid artifact output path')
        if isinstance(node.func,ast.Attribute):
            attr = node.func.attr
            if isinstance(node.func.value,ast.Name):
                module = aliases.get(node.func.value.id)
                if module in MODULE_CALLS and attr not in MODULE_CALLS[module]:
                    raise UnsafeCode('Module function not allowed')
            if attr in {'savefig','to_csv','to_json'}:
                target = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg in {'fname','path_or_buf'}),None)
                if target is None and attr == 'to_json':
                    continue
                if not isinstance(target,ast.Constant) or not isinstance(target.value,str):
                    raise UnsafeCode('Artifact output must be a literal relative filename')
                filename = target.value
                if not filename or '/' in filename or '\\' in filename or ':' in filename or filename.startswith('.') or Path(filename).suffix.lower() not in {'.csv','.png','.json','.txt'}:
                    raise UnsafeCode('Invalid artifact output path')
    return tree


def re_path(value):
    import re
    return bool(re.match(r'^(?:[a-zA-Z]:[/\\]|[/\\]|https?://|file:)',value))


class AnalysisRuntime:
    def __init__(self, store, config=None):
        self.store = store
        self.config = config or {}

    def run(self, workspace_id, request, *, task_id=None, step_id=None):
        security=getattr(self.store,'security',None)
        if security:
            from server.auth.context import require_principal
            from fastapi import HTTPException
            principal=require_principal()
            security.authorize(principal,'analysis.execute',workspace_id)
            if task_id and security.task(task_id,'task.execute',principal)['workspace_id']!=workspace_id:
                raise HTTPException(403,'Task workspace mismatch')
        assets = [self.store.get(workspace_id, dataset_id) for dataset_id in request.dataset_ids]
        execution_id = identity()
        result = CodeExecutionResult(execution_id=execution_id,workspace_id=workspace_id,task_id=task_id,status='failed',
                                     objective=request.objective,dataset_ids=request.dataset_ids,code_hash=hashlib.sha256(request.code.encode()).hexdigest())
        # Artifacts reference this receipt through a real PostgreSQL foreign key.
        # Persist the provisional result before registering any child records.
        self.store.save_execution(result.model_dump())
        start = time.monotonic()
        directory = self.store.root/'data_analysis'/execution_id
        directory.mkdir(parents=True)
        # Preserve rejected code for local audit, without ever executing it.
        (directory/'code.py').write_text(request.code[:self.config.get('ANALYSIS_MAX_CODE_CHARS',12000)],encoding='utf-8')
        try:
            validate_code(request.code,self.config.get('ANALYSIS_MAX_CODE_CHARS',12000))
        except (SyntaxError,UnsafeCode) as error:
            result.error_type = type(error).__name__
            result.stderr = str(error)[:500]
            self.store.save_execution(result.model_dump())
            return result
        datasets = {}
        for asset in assets:
            copied = directory/('dataset_'+asset.id+'.'+asset.file_type)
            shutil.copyfile(self.store.checked_path(asset.file_path),copied)
            datasets[asset.id] = copied.name
        (directory/'code.py').write_text(request.code,encoding='utf-8')
        (directory/'input.json').write_text(json.dumps({'datasets':datasets,'max_artifact_bytes':self.config.get('ANALYSIS_MAX_ARTIFACT_MB',20)*1024*1024}),encoding='utf-8')
        child = Path(__file__).with_name('analysis_worker.py')
        env = {key: os.environ[key] for key in ('SYSTEMROOT','WINDIR','TEMP','TMP') if key in os.environ}
        env.update(MPLBACKEND='Agg',MPLCONFIGDIR=str(directory/'mplconfig'),OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1')
        try:
            process = subprocess.Popen([sys.executable,'-I',str(child)],cwd=directory,env=env,shell=False,
                                       stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        except OSError as error:
            result.error_type = 'ExecutionUnavailable'
            result.stderr = type(error).__name__
            self.store.save_execution(result.model_dump())
            return result
        buffers = [bytearray(),bytearray()]
        output_limit = self.config.get('ANALYSIS_MAX_OUTPUT_CHARS',20000)
        def drain(pipe, buffer):
            while True:
                data = pipe.read(4096)
                if not data:
                    break
                if len(buffer) < output_limit:
                    buffer.extend(data[:output_limit-len(buffer)])
            pipe.close()
        readers = [Thread(target=drain,args=(pipe,buffer),daemon=True) for pipe,buffer in zip((process.stdout,process.stderr),buffers)]
        for reader in readers:
            reader.start()
        initial = {p.name for p in directory.iterdir()}
        error_type = None
        timeout = self.config.get('ANALYSIS_TIMEOUT_SECONDS',30)
        max_size = self.config.get('ANALYSIS_MAX_ARTIFACT_MB',20)*1024*1024
        max_count = self.config.get('ANALYSIS_MAX_ARTIFACTS',10)
        while process.poll() is None:
            outputs = [p for p in directory.iterdir() if p.is_file() and p.name not in initial]
            if time.monotonic()-start > timeout:
                error_type = 'ExecutionTimeout'
            elif len(outputs) > max_count or sum(p.stat().st_size for p in outputs) > max_size:
                error_type = 'ArtifactLimitExceeded'
            if error_type:
                process.kill()
                break
            time.sleep(.05)
        process.wait(timeout=5)
        for reader in readers:
            reader.join(timeout=5)
        result.stdout = buffers[0].decode('utf-8',errors='replace')[:output_limit]
        result.stderr = buffers[1].decode('utf-8',errors='replace')[:output_limit]
        result.error_type = error_type
        if process.returncode and not error_type:
            result.error_type = 'DependencyUnavailable' if 'ModuleNotFoundError' in result.stderr else next((kind for kind in ('SyntaxError','NameError','DependencyUnavailable','MemoryError','ValueError','TypeError') if kind in result.stderr),'ExecutionError')
        outputs = [p for p in directory.iterdir() if p.is_file() and p.name not in initial]
        if len(outputs)>max_count or sum(p.stat().st_size for p in outputs)>max_size:
            result.error_type = 'ArtifactLimitExceeded'
        if not result.error_type:
            try:
                for path in outputs:
                    if path.suffix.lower() not in {'.png','.csv','.json','.txt'} or path.is_symlink():
                        raise ValueError('Unsupported artifact')
                    artifact = Artifact(execution_id=execution_id,task_id=task_id,step_id=step_id,workspace_id=workspace_id,
                                        filename=path.name,artifact_type=path.suffix[1:],mime_type=mimetypes.guess_type(path.name)[0] or 'text/plain',file_path=str(path),size_bytes=path.stat().st_size)
                    self.store.register_artifact(artifact)
                    result.artifacts.append(artifact.model_dump(exclude={'file_path'}))
                result.status = 'completed'
            except Exception as error:
                LOGGER.warning('Artifact registration failed error_type=%s',type(error).__name__)
                result.error_type = 'ArtifactWriteFailure'
        result.duration_ms = round((time.monotonic()-start)*1000,2)
        # Tracebacks may expose only the child working directory; return a short error summary.
        if result.error_type:
            result.stderr = result.stderr.splitlines()[-1][:500] if result.stderr else result.error_type
        self.store.save_execution(result.model_dump())
        return result
