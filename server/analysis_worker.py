"""Internal child-process entry point; never import this in the API process."""
import builtins
import json
from pathlib import Path


def main():
    import pandas as pd
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        plt = None
    settings = json.loads(Path('input.json').read_text(encoding='utf-8'))
    mapping = settings['datasets']
    datasets, sheets = {}, {}
    for dataset_id, filename in mapping.items():
        suffix = Path(filename).suffix
        if suffix == '.xlsx':
            tables = pd.read_excel(filename,sheet_name=None,engine='openpyxl')
        elif suffix == '.csv':
            tables = {'data':pd.read_csv(filename,encoding='utf-8-sig')}
        else:
            value = json.loads(Path(filename).read_text(encoding='utf-8-sig'))
            tables = {'data':pd.DataFrame(value)}
        sheets[dataset_id] = tables
        datasets[dataset_id] = next(iter(tables.values()))
    allowed_imports = {'pandas','numpy','matplotlib.pyplot','statistics','math','json','csv','re','datetime','collections'}
    # NumPy's C-level ndarray reductions lazily request these implementation
    # modules using the caller's builtins. Generated imports still fail AST policy.
    internal_imports = {'numpy._core._methods','numpy.core._methods'}
    def safe_import(name,globals=None,locals=None,fromlist=(),level=0):
        if name not in allowed_imports | internal_imports or level:
            raise ImportError('DependencyUnavailable or forbidden import: '+name)
        try:
            return builtins.__import__(name,globals,locals,fromlist,level)
        except ImportError as error:
            raise ImportError('DependencyUnavailable') from error
    names = ['print','len','range','enumerate','zip','list','dict','set','tuple','str','int','float','bool','sum','min','max','sorted','round','abs','all','any','Exception','ValueError']
    safe_builtins = {name:getattr(builtins,name) for name in names}
    safe_builtins['__import__'] = safe_import
    def write_artifact(filename, content):
        if not isinstance(filename,str) or not isinstance(content,str) or Path(filename).name != filename or '\\' in filename or ':' in filename or filename.startswith('.') or Path(filename).suffix not in {'.json','.txt','.csv'}:
            raise ValueError('Invalid artifact output')
        path = Path(filename)
        if path.is_symlink() or path.resolve().parent != Path.cwd():
            raise ValueError('Artifact path outside execution directory')
        data = content.encode('utf-8')
        if len(data)>settings['max_artifact_bytes']:
            raise ValueError('Artifact size limit exceeded')
        path.write_bytes(data)
    namespace = {'__builtins__':safe_builtins,'datasets':datasets,'sheets':sheets,'df':next(iter(datasets.values())),'write_artifact':write_artifact}
    try:
        exec(compile(Path('code.py').read_text(encoding='utf-8'),'analysis','exec'),namespace,namespace)
    finally:
        if plt is not None:
            plt.close('all')


if __name__ == '__main__':
    main()
