FROM ghcr.io/astral-sh/uv:0.12.21 AS uv
FROM python:3.11-slim-bookworm
COPY --from=uv /uv /uvx /usr/local/bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    HOME=/data/home HF_HOME=/data/models MPLCONFIGDIR=/tmp/matplotlib
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home app \
    && mkdir -p /app /data/workspace /data/home /data/models && chown -R app:app /data
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
ARG INSTALL_OCR=false
RUN if [ "$INSTALL_OCR" = true ]; then uv sync --frozen --no-dev --no-install-project --extra ocr; \
    else uv sync --frozen --no-dev --no-install-project; fi
COPY server ./server
COPY client ./client
COPY alembic ./alembic
COPY alembic.ini ./
USER 10001:10001
EXPOSE 8001 8501
HEALTHCHECK --interval=15s --timeout=5s --start-period=90s CMD python -m server.healthcheck api
CMD ["python", "-m", "server.api"]
