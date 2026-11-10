# VERTICE backend image. Build context: the repository root (see docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir uv==0.11.32

# Dependencies first, so editing code does not reinstall them.
WORKDIR /app/backend
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN uv sync --frozen --no-dev

COPY backend/ ./
COPY config/ /app/config/

# Run as an unprivileged user; the database lives in /app/data (a volume).
RUN useradd --create-home --uid 1000 vertice \
    && mkdir -p /app/data \
    && chown -R vertice:vertice /app/data
USER vertice

ENV DATABASE_URL=sqlite:////app/data/vertice.db
EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
