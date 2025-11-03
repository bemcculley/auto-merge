# syntax=docker/dockerfile:1
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_SYSTEM_PYTHON=1

# Install uv (fast Python package manager)
RUN pip install --no-cache-dir uv

WORKDIR /app

# Copy project files
COPY pyproject.toml .
COPY app ./app

# Install deps
# Simpler and more portable than process substitution in sh
RUN uv pip install . --system

EXPOSE 8080

# Create a non-root user
RUN useradd -m appuser
USER appuser

# Start the server (single worker by default for metrics simplicity)
# Use gunicorn+uvicorn or plain uvicorn; here we use uvicorn
ENV HOST=0.0.0.0 PORT=8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
