FROM python:3.11-slim

# pyproj/shapely 均带 manylinux wheel，无需编译；curl 用于 compose 健康检查
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY app /app/app

# 持久化 SQLite 数据目录
RUN mkdir -p /data
ENV DB_PATH=/data/weighting.db \
    PYTHONUNBUFFERED=1 \
    POSITIVE_AREA_EPS_M2=1e-6

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=5 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
