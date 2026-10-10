FROM node:24-bookworm-slim AS frontend-build
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=10000 \
    DOTA_DB_PATH=/app/.cache/market.sqlite3
WORKDIR /app
COPY backend/requirements.lock.txt ./backend/requirements.lock.txt
RUN pip install --no-cache-dir -r backend/requirements.lock.txt
COPY backend/ ./backend/
COPY gunicorn.conf.py ./
COPY --from=frontend-build /build/frontend/dist ./frontend/dist
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/.cache \
    && chown appuser:appuser /app/.cache
USER appuser
EXPOSE 10000
CMD ["gunicorn", "--config", "gunicorn.conf.py", "backend.wsgi:app"]
