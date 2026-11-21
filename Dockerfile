# syntax=docker/dockerfile:1

FROM python:3.12-slim AS build
WORKDIR /src
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY pyproject.toml README.md LICENSE ./
COPY app ./app
RUN pip wheel --wheel-dir /wheels .

FROM python:3.12-slim
LABEL org.opencontainers.image.title="gira-whatsapp-agent" \
      org.opencontainers.image.description="WhatsApp host for GIRA — Pasta & Mare" \
      org.opencontainers.image.licenses="MIT"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=production PORT=8000
WORKDIR /app

RUN useradd --create-home --uid 10001 agent
COPY --from=build /wheels /wheels
RUN pip install /wheels/* && rm -rf /wheels
COPY alembic.ini ./
COPY migrations ./migrations
COPY config ./config

USER agent
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=4)"

# Apply database migrations, then serve. One worker: the safety-net scheduler runs in-process.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
