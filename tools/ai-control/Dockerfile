FROM python:3.11-slim@sha256:9a7765b36773a37061455b332f18e265e7f58f6fea9c419a550d2a8b0e9db834

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system --gid 1001 appuser \
    && adduser  --system --uid 1001 --gid 1001 --no-create-home --shell /bin/false appuser

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.5.1+cpu \
    && python -m pip install --no-cache-dir -r /app/requirements.txt

COPY app.py app_storage.py storage_migrations.py /app/

# /app is read-only at runtime (read_only: true in docker-compose).
# All runtime writes go to volume-mounted paths (/data, /state, /chroma_db, /chroma_db_es).
# Docker socket access is disabled in the default compose file. Trusted
# operator machines can opt in with docker-compose.runtime-control.yaml.
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8081

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8081"]
