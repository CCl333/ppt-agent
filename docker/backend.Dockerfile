FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates fontconfig fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /data /app/storage/uploads /app/storage/backgrounds /app/storage/exports \
    && fc-cache -f

COPY backend /app/backend
COPY fonts /app/fonts

RUN pip install --no-cache-dir -e /app/backend \
    && python -c "import fitz, pptx; print('pymupdf', fitz.version)"

ENV PYTHONPATH=/app/backend
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["sh", "-c", "mkdir -p /data /app/storage/uploads /app/storage/backgrounds /app/storage/exports && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
