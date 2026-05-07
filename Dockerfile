FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends wget && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
COPY frontend/ ./frontend/

# Stamp the service worker with a unique build version so stale caches are
# evicted automatically on every new image — no manual version bumping needed.
RUN sed -i "s/CACHE_VERSION_PLACEHOLDER/$(date +%Y%m%d%H%M%S)/" frontend/sw.js

EXPOSE 8080

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8080"]
