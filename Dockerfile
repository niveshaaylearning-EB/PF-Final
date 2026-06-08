# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — Build both React frontends (Node alpine, discarded after build)
# ─────────────────────────────────────────────────────────────────────────────
FROM node:20-alpine AS build-frontend

WORKDIR /build

COPY frontend/package*.json ./frontend/
RUN cd frontend && npm ci --silent
COPY frontend/ ./frontend/
RUN cd frontend && npm run build

COPY webportal/frontend/package*.json ./webportal/frontend/
RUN cd webportal/frontend && npm ci --silent
COPY webportal/frontend/ ./webportal/frontend/
RUN cd webportal/frontend && npm run build


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — Final runtime (Python + supervisor only, no nginx)
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

WORKDIR /app

# supervisor + build tools for scipy/nsepython
RUN apt-get update && apt-get install -y --no-install-recommends \
        supervisor gcc g++ gfortran libopenblas-dev liblapack-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Remove build tools after pip
RUN apt-get purge -y --auto-remove gcc g++ gfortran libopenblas-dev liblapack-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY backend/         ./backend/
COPY webportal/backend/ ./webportal/backend/

COPY --from=build-frontend /build/frontend/dist           ./frontend/dist
COPY --from=build-frontend /build/webportal/frontend/dist ./webportal/frontend/dist

COPY supervisord.conf /etc/supervisor/conf.d/nia.conf

# Render routes external traffic to port 8000 (main backend)
# Webportal runs internally on 8001, proxied by main backend at /wp/
EXPOSE 8000

CMD ["supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
