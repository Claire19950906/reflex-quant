#!/usr/bin/env bash
# =============================================================
# Trident v300 — 构建「体验镜像」（demo / licensed 模式）
# =============================================================
# 关键点：
#   1) 多阶段 build：builder 装依赖 + 编译 .py → .pyc；
#      runtime 只 COPY .pyc + runtime deps，**不 COPY** 源码 .py
#   2) 默认 ENV TRIDENT_MODE=demo（CI 可改 licensed）
#   3) 不挂载 engine/ 源码目录（只挂编译产物）
#
# 用法：
#   ./scripts/build_demo_image.sh demo v300
#   ./scripts/build_demo_image.sh licensed v300
# =============================================================
set -euo pipefail

MODE="${1:-demo}"      # demo | licensed
TAG="${2:-v300}"       # image tag
NAME="trident-${MODE}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

if [[ "$MODE" != "demo" && "$MODE" != "licensed" ]]; then
    echo "usage: $0 [demo|licensed] [tag]" >&2
    exit 1
fi

echo "==> [build] mode=$MODE tag=$TAG"

# 1) builder 阶段：装 deps + 把 src_python/ 编译成 .pyc
TMP_DOCKERFILE="$(mktemp -t dockerfile-demo.XXXXXX)"
cat > "$TMP_DOCKERFILE" <<EOF
# syntax=docker/dockerfile:1.6
FROM python:3.11-slim AS builder
WORKDIR /app

# 1.1 装运行时依赖（只装 wheel，跳过源码 .py 的 COPY）
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip wheel --no-cache-dir --wheel-dir /wheels \\
    -r /app/backend/requirements.txt

# 1.2 复制 src_python 源码（builder 内，runtime 不会带过去）
COPY backend/src_python /app/backend/src_python

# 1.3 编译成字节码：删除 .py，只留 __pycache__/*.pyc
RUN find /app/backend/src_python -name "__pycache__" -type d -exec rm -rf {} + \\
 && python -m compileall -b /app/backend/src_python \\
 && find /app/backend/src_python -name "*.py" -delete \\
 && find /app/backend/src_python -name "*.pyc" -exec mv {} {%.pyc}.pyc \\;

# 2) runtime 阶段：只装 wheels + 编译后的 .pyc
FROM python:3.11-slim AS runtime
WORKDIR /app

# 2.1 必备系统库（sqlite / tzdata / ca-certs）
RUN apt-get update && apt-get install -y --no-install-recommends \\
        ca-certificates tzdata sqlite3 \\
    && rm -rf /var/lib/apt/lists/*

# 2.2 装 wheels
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels \\
        fastapi uvicorn[standard] pydantic httpx 2>/dev/null || \\
    pip install --no-cache-dir --find-links=/wheels /wheels/*.whl

# 2.3 复制编译后的代码（**没有 .py 源码**）
COPY --from=builder /app/backend/src_python /app/backend/src_python

# 2.4 复制 scripts/ 头部（gen_license 等只 admin 用，runtime 不需要）
#     实际不需要：仅 COPY docs/ 给容器内 help 用
COPY docs /app/docs

# 2.5 创建非 root 用户
RUN useradd -m -u 10001 trident \\
 && mkdir -p /var/lib/trident && chown -R trident:trident /var/lib/trident
USER trident

# 2.6 默认 ENV（注意：TRIDENT_LICENSE_SECRET 必须用 docker run -e 注入）
ENV TRIDENT_MODE=${MODE} \\
    TRIDENT_LISTEN_HOST=0.0.0.0 \\
    TRIDENT_LISTEN_PORT=8000 \\
    PYTHONDONTWRITEBYTECODE=1 \\
    PYTHONUNBUFFERED=1

EXPOSE 8000

# 2.7 启动：直接 uvicorn 跑 .pyc（python 透明支持）
CMD ["sh", "-c", "python -m uvicorn api_server:app --host 0.0.0.0 --port 8000 --app-dir /app/backend/src_python"]
EOF

# 2) 真正 docker build
cd "$ROOT"
docker build -f "$TMP_DOCKERFILE" -t "${NAME}:${TAG}" .
BUILD_RC=$?
rm -f "$TMP_DOCKERFILE"

if [[ $BUILD_RC -ne 0 ]]; then
    echo "==> [build] FAILED (exit $BUILD_RC)" >&2
    exit $BUILD_RC
fi

echo "==> [build] OK: ${NAME}:${TAG}"
echo
echo "下一步（demo 模式，本地试跑）："
echo "  docker run --rm -p 8000:8000 ${NAME}:${TAG}"
echo
echo "下一步（licensed 模式，挂载 license + api key）："
echo "  docker run --rm -p 8000:8000 \\"
echo "    -e TRIDENT_MODE=licensed \\"
echo "    -e TRIDENT_LICENSE_SECRET=\$(openssl rand -hex 32) \\"
echo "    -v \$(pwd)/license.lic:/etc/trident/license.lic:ro \\"
echo "    -v \$(pwd)/api_keys.json:/etc/trident/api_keys.json:ro \\"
echo "    ${NAME}:${TAG}"
echo
echo "推到远端："
echo "  docker tag ${NAME}:${TAG} ghcr.io/yourname/${NAME}:${TAG}"
echo "  docker push ghcr.io/yourname/${NAME}:${TAG}"
