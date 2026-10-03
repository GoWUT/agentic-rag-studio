"""Existing mock MCP catalogue, with a post-send ambiguous-write delay fixture."""
import json
import os
from pathlib import Path
import time
from evaluation.phase4_mock_server import build_server

if __name__=='__main__':
    folder=Path(os.environ['PHASE5B1_SMOKE_FOLDER'])
    ledger=folder/'actions.json'
    original_write=Path.write_text
    def write(self,*args,**kwargs):
        result=original_write(self,*args,**kwargs)
        if self.resolve()==ledger.resolve():
            settings=json.loads((folder/'control.json').read_text())
            if settings.get('ambiguous_write'):
                original_write(folder/'write_sent',str(os.getpid()))
                time.sleep(45)  # local side effect happened; response/checkpoint withheld
        return result
    Path.write_text=write
    build_server(ledger).run()
