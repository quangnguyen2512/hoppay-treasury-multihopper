FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    PORT=8790

COPY pyproject.toml README.md ARCHITECTURE.md ./
COPY src ./src
COPY signer ./signer

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir .

EXPOSE 8790

CMD ["sh", "-c", "uvicorn hoppay.app:create_app --factory --host 0.0.0.0 --port ${PORT:-8790}"]
