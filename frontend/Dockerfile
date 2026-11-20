# ── 前端镜像（P2-4 容器化骨架）─────────────────────────────────────────────
# 多阶段：node 构建 → 仅带依赖运行。Next 14 标准 `next start`。
#
# 构建：
#   docker build -t trident-frontend -f frontend/Dockerfile frontend/
#
# 运行：
#   docker run --rm -p 127.0.0.1:3030:3030 \
#     -e NEXT_PUBLIC_API_URL=http://127.0.0.1:8100/api \
#     trident-frontend

FROM node:20-slim AS builder
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM node:20-slim AS runner
WORKDIR /app
ENV NODE_ENV=production \
    PORT=3030
COPY package.json package-lock.json ./
RUN npm ci --omit=dev
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/public ./public
COPY --from=builder /app/next.config.* ./
USER node
EXPOSE 3030
CMD ["npm", "run", "start"]
