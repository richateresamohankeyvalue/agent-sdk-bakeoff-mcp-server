FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY server/ ./server/
COPY data/ ./data/
COPY README.md ./
RUN uv sync --frozen --no-dev

ENV PYTHONUNBUFFERED=1 \
    MCP_TRANSPORT=sse \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8081

EXPOSE 8081

# /sse itself streams forever, so a plain curl there would hang until
# --max-time and read as unhealthy even when the server is fine. /messages/
# is a normal (non-streaming) POST endpoint — a GET against it returns
# immediately (405), which is enough to prove the server is up and speaking
# HTTP. (Assumes the default MCP_TRANSPORT=sse; adjust if you override it.)
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -s --max-time 3 -o /dev/null -w '%{http_code}' "http://localhost:${MCP_PORT}/messages/" | grep -qE '^[0-9]{3}$' || exit 1

CMD ["uv", "run", "pulse-assistant-mcp"]
