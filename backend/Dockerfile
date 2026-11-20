# ── 后端镜像（P2-4 容器化骨架）─────────────────────────────────────────────
# 说明：这是可复现构建的骨架。当前生产仍以 launchd 本机方式运行；
# 本文件用于把主服务迁到容器，依赖以 requirements-lock.txt 锁定基线安装。
#
# 构建：
#   docker build -t trident-backend -f backend/Dockerfile backend/
#
# 运行（bridge 主服务，库/日志/runtime 挂卷持久化）：
#   docker run --rm -p 127.0.0.1:8100:8100 \
#     -v "$PWD/backend/runtime:/app/runtime" \
#     -v "$PWD/backend/reports:/app/reports" \
#     -v "$PWD/backend:/app/data" \
#     trident-backend
#
# 注意：
#   * SQLite 库文件必须挂到持久卷，容器重建不丢数据；
#   * 本地 Ollama（11434）当前跑在宿主机，容器内需用 host.docker.internal 访问；
#   * 117 个 launchd cron / 守护进程不在本镜像内，迁移到容器前先按
#     docs/ops/容器化迁移说明.md 逐项归类。

FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src_python \
    TRIDENT_DB_PATH=/app/data/trident_event_bus.db

WORKDIR /app

# 系统库：lxml / pillow / numpy / pandas 的 wheel 多数自带，留最小基础包。
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 先拷依赖清单，利用层缓存。锁文件固定已验证版本（Python 3.14.5 基线）。
COPY requirements-lock.txt ./
RUN pip install --no-cache-dir -r requirements-lock.txt

# 再拷源码。
COPY src_python ./src_python
COPY scripts ./scripts

# 非 root 运行。
RUN useradd -m appuser \
    && mkdir -p /app/runtime /app/reports /app/data \
    && chown -R appuser:appuser /app
USER appuser

# bridge 主服务（8100）。master 服务（9001）用同镜像、覆盖启动命令：
#   docker run ... trident-backend python src_python/master_api.py
EXPOSE 8100 9001

# 健康检查：打回环 /api/health。容器内连宿主机回环用 host 网关时另配。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/api/health',timeout=5).status==200 else 1)"

CMD ["python", "src_python/bridge_server.py"]
