# Builds the control-plane. Build context = the repo root (web/ and admin/ must sit next to app/).
FROM python:3.12-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY control-plane/ /app/control-plane/
COPY web/ /app/web/
COPY admin/ /app/admin/

RUN pip install --no-cache-dir -e "/app/control-plane[postgres]"

# unprivileged user: the app does NOT write to disk (state lives only in Postgres, logs go to
# stdout), so chown /app is unnecessary — read access is enough. alembic and uvicorn live in
# /usr/local/bin (world-exec). This shrinks the blast radius of an RCE.
RUN useradd --create-home --uid 10001 appuser
USER appuser

WORKDIR /app/control-plane
EXPOSE 8000
# apply the migrations and bring the API up
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
