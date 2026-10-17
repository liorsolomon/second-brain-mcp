FROM python:3.14-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.7.17 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.14-slim
RUN useradd --create-home --uid 10001 brain
WORKDIR /app
# Code stays root-owned (read-only to the server). No USER line: the entrypoint
# starts as root to prepare the volume, then drops to `brain` before the server runs.
COPY --from=build /app /app
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    SB_TRANSPORT=http \
    SB_STORAGE=s3 \
    SB_AUTH=google \
    FASTMCP_SHOW_SERVER_BANNER=false \
    FASTMCP_CHECK_FOR_UPDATES=off
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"SB_PORT\",\"8000\")}/health')"
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["second-brain-mcp"]
