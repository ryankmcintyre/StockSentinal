FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    LOG_LEVEL=INFO \
    DATABASE_URL=sqlite:////data/stocksentinal.db

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser \
    && mkdir -p /data \
    && chown appuser:appuser /app /data

COPY --chown=appuser:appuser pyproject.toml README.md ./
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser alembic.ini ./

RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir .

USER appuser

EXPOSE 8000
VOLUME ["/data"]

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
