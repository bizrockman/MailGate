# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip build && \
    pip install --target /install .


FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/lib

RUN groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app
WORKDIR /app

COPY --from=builder /install /app/lib

USER app
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
    CMD python -c "import os,urllib.request,sys; \
        k=os.environ.get('MAILGATE_CLIENT_API_KEY',''); \
        req=urllib.request.Request('http://127.0.0.1:8080/health', headers={'Authorization':'Bearer '+k}); \
        urllib.request.urlopen(req, timeout=2); sys.exit(0)" || exit 1

CMD ["python", "-m", "mailgate.main"]
