#changes made by me
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

# supervisor + build tools for scipy/nsepython + libreoffice (headless, for
# converting an uploaded legacy .doc result-update into .docx so it can be
# parsed -- python-docx only reads the modern .docx XML format, and Word's
# own COM automation (used for this exact conversion in local Windows dev)
# doesn't exist on Linux, so headless LibreOffice is the production
# equivalent: `soffice --headless --convert-to docx`.
RUN apt-get update && apt-get install -y --no-install-recommends \
    supervisor gcc g++ gfortran libopenblas-dev liblapack-dev pkg-config \
    libreoffice-writer \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
# Advisory vulnerability scan — prints findings but never blocks the build
RUN pip install --no-cache-dir pip-audit && \
    pip-audit -r backend/requirements.txt --desc on 2>&1 || true && \
    pip uninstall -y pip-audit pip-audit-requirements 2>/dev/null || true

# `pip install playwright` only installs the Python driver, never the actual
# browser binary -- without this, every live smallcase/competitor-login call
# fails with "Executable doesn't exist at /root/.cache/ms-playwright/...",
# confirmed live 2026-10-22. --with-deps also pulls in the OS-level shared
# libraries headless Chromium needs (nss, libatk, etc.), so it must run
# while apt is still usable, before the build-tool purge below.
RUN playwright install --with-deps chromium

# Remove build tools after pip
RUN apt-get purge -y --auto-remove gcc g++ gfortran libopenblas-dev liblapack-dev pkg-config \
    && rm -rf /var/lib/apt/lists/*

COPY backend/         ./backend/
COPY webportal/backend/ ./webportal/backend/

COPY --from=build-frontend /build/frontend/dist           ./frontend/dist
COPY --from=build-frontend /build/webportal/frontend/dist ./webportal/frontend/dist

COPY supervisord.conf /etc/supervisor/conf.d/nia.conf
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

EXPOSE 8000

CMD ["/app/start.sh"]
