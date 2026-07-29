# =============================================================================
# fastapi-gateway — my DSAR API in front of Fides.
# =============================================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

# Dependencies first so the layer caches across app edits.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

EXPOSE 8000

# Shell form so GATEWAY_LOG_LEVEL is interpolated at runtime.
# Swagger UI is then at http://localhost:8000/docs
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --log-level ${GATEWAY_LOG_LEVEL:-info}"]
