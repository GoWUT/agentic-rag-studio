"""Container probes: never print environment, database URLs or exception text."""
import os
import sys
from urllib.request import urlopen


def main():
    mode=sys.argv[1] if len(sys.argv)>1 else 'api'
    try:
        if mode=='worker':
            import json
            import time
            from pathlib import Path
            path=Path(os.environ['WORKER_HEALTH_FILE'])
            if time.time()-path.stat().st_mtime>15:return 1
            identifier=json.loads(path.read_text())['worker_id']
            if not isinstance(identifier,int):return 1
            from sqlalchemy import create_engine,text
            from sqlalchemy.pool import NullPool
            from server.db.runtime import driver_url
            engine=create_engine(driver_url(os.environ['DATABASE_URL']),poolclass=NullPool,hide_parameters=True,connect_args={'connect_timeout':3})
            try:
                with engine.connect() as db:
                    present=db.scalar(text("SELECT count(*) FROM procrastinate_workers WHERE last_heartbeat > now() - interval '30 seconds' AND id=:id"),{'id':identifier})
                return 0 if present else 1
            finally:engine.dispose()
        url='http://127.0.0.1:8501/_stcore/health' if mode=='streamlit' else 'http://127.0.0.1:8001/health/ready'
        with urlopen(url,timeout=3) as response:return 0 if response.status==200 else 1
    except Exception:return 1


if __name__=='__main__':sys.exit(main())
