# Backend image — targets Google Cloud Run.
#
# Build and deploy from the repo root:
#   gcloud run deploy storefront-backend --source backend --region europe-west2
#
# Two Cloud Run flags are not optional for this service — see DEPLOY.md:
#   --no-cpu-throttling   the pipeline runs in a background task after the HTTP
#                         response is sent. Cloud Run throttles CPU to ~0 once a
#                         response returns, so without this a run starts, hands
#                         back a run_id, and then freezes for ever.
#   --max-instances=1     the one-run-at-a-time and rate limits are per-process.
#                         More instances means more concurrent runs, and this
#                         endpoint spends money.

FROM python:3.11-slim

# - PYTHONUNBUFFERED: logs reach Cloud Logging immediately instead of sitting in
#   a buffer, which matters when a run takes minutes and you are watching it.
# - PYTHONDONTWRITEBYTECODE: the filesystem is ephemeral; .pyc files are waste.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Requirements first: this layer only rebuilds when dependencies change, not on
# every code edit.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The application. data/products holds the client's three reference photos — the
# compositing stage cannot run without them, so they ship in the image.
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY data/ ./data/

# Scratch space for in-flight images and Gemini traces. Ephemeral on Cloud Run
# and that is fine: the database and storage bucket are the durable record.
# Created up front and owned by the runtime user so the app never has to chown.
RUN mkdir -p /app/outputs /app/.cache

# Don't run as root.
RUN useradd --create-home --uid 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Cloud Run injects PORT (8080). Default it so `docker run -p 8080:8080 img`
# works locally too.
ENV PORT=8080
EXPOSE 8080

# Shell form so ${PORT} expands. One worker on purpose: the run registry and the
# rate limiter live in process memory, so a second worker would mean two
# pipelines running at once and double the bill.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers 1
