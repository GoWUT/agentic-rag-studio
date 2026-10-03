"""Cross-platform API launcher with psycopg-compatible Windows event loop."""
import asyncio
import os


def selector_loop():
    # Uvicorn >=0.36 chooses Proactor on Windows even when loop='asyncio'.
    # Keep the process default policy intact: synchronous MCP calls need a
    # Proactor loop for async subprocess transports in their own threads.
    return asyncio.SelectorEventLoop()

if __name__ == '__main__':
    import uvicorn
    uvicorn.run('server.main:app', host=os.getenv('API_HOST','127.0.0.1'),
                port=int(os.getenv('API_PORT','8001')), loop='server.api:selector_loop')
