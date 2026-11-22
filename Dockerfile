# ==========================================================================
#  KaiAI — Docker image
#  Build:  docker build -t kaiai .
#  Run  :  docker run --env-file .env -p 8080:8080 -v kaiai-data:/app/data kaiai
# ==========================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KAI_DATA_DIR=/app/data \
    PORT=8080

WORKDIR /app

# Cài dependencies trước để tận dụng cache của Docker layer.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy toàn bộ mã nguồn.
COPY . .

# Chạy bằng user không phải root để tăng bảo mật.
RUN adduser --disabled-password --gecos "" --uid 10001 kaiai \
    && mkdir -p /app/data \
    && chown -R kaiai:kaiai /app
USER kaiai

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health').status==200 else 1)"

CMD ["python", "main.py"]
