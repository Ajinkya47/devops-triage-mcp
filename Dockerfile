ARG UV_VERSION=latest
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:3.13-slim
COPY --from=uv /uv /bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev

COPY . .

RUN useradd --system --create-home --uid 10001 app \
    && mkdir /data \
    && chown app /data

ENV PATH="/app/.venv/bin:$PATH" \
    BUGS_DB_PATH=/data/bugs.db \
    ACTIONS_DB_PATH=/data/actions.db \
    AUDIT_DB_PATH=/data/audit.db

USER app
ENTRYPOINT ["python", "agent.py"]