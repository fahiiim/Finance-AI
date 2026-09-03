FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# curl is used by HEALTHCHECK below. Installed before the dependency layer so
# it is not reinstalled every time requirements.txt changes.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# scripts/ ships too: verify_backend_contract.py is how the backend team
# checks their endpoints, and it is most useful run from inside this image,
# against the same network and the same environment the service will see.
COPY scripts ./scripts

RUN useradd --create-home --shell /bin/bash appuser
USER appuser

EXPOSE 8000

# Liveness only. /ready is the readiness probe and checks the backend as well
# -- point the orchestrator's readiness probe at that, and its liveness probe
# here. Wiring liveness to /ready would turn one backend outage into a
# fleet-wide restart loop that fixes nothing.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8000/health || exit 1

# ONE WORKER, DELIBERATELY.
#
# Conversation history lives in process memory until CONVERSATION_STORAGE is
# set to backend_api (docs/handover-backend.md section 8). A second worker is a
# second process with its own memory, so a user's follow-up turn can land on a
# worker that has never heard of them: the assistant re-asks a question it was
# already answered and loses the period under discussion. Nothing errors and
# every response is still a 200, which is exactly why this is written down
# rather than left to whoever next edits this line.
#
# Once conversations are persisted, raise --workers freely.
#
# --proxy-headers so the client IP survives a load balancer; --forwarded-allow-ips
# should name the proxy in production rather than trusting every hop.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--workers", "1", \
     "--proxy-headers"]
