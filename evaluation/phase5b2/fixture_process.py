"""Evaluation only: real PDF validation/storage, deterministic embedding/model ports."""
import argparse
import asyncio
import os
from pathlib import Path


def install():
    from evaluation.phase5b1.fixture_process import install_fixture
    install_fixture()
    from server.rag.ingestion import ChromaIndexAdapter
    def build(self,pdf_path,index_dir):
        index_dir.mkdir(parents=True,exist_ok=True)
        (index_dir/'evaluation-only.index').write_text('Synthetic embedding index; PDF ingestion is real')
    ChromaIndexAdapter.build=build
    ChromaIndexAdapter.exists=lambda self,path:(path/'evaluation-only.index').exists()
    ChromaIndexAdapter.load=lambda self,path:None


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('mode',choices=['api','worker']); args=parser.parse_args()
    install()
    if args.mode=='api':
        import uvicorn
        uvicorn.run('server.main:app',host='127.0.0.1',port=int(os.getenv('API_PORT','8037')),loop='server.api:selector_loop')
    else:
        from server.worker import main
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:runner.run(main())
