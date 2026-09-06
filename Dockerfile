# bankrag: FastAPI app + Studio. Data (skills, knowledge, evals, pricing, SQLite) lives on a mounted volume at /data.
FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra import
COPY src ./src
COPY web ./web
COPY skills ./skills
COPY knowledge ./knowledge
COPY evals ./evals
COPY pricing.yaml README.md ./
COPY docker/entrypoint.sh /entrypoint.sh
RUN uv sync --frozen --no-dev --extra import && chmod +x /entrypoint.sh && mkdir -p /data
ENV PATH="/app/.venv/bin:$PATH" DATA_DIR=/data API_PORT=8010
EXPOSE 8010
ENTRYPOINT ["/entrypoint.sh"]
