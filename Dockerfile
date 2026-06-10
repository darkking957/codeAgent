# CoreAgent Web（#0025 多用户 / 安全 / 部署）单容器镜像。
#
# 关键：执行级 OS 沙箱用 **bubblewrap**（系统级依赖，apt 装），把 run_command / grep 子进程关进
# 会话 workspace（断网 / 只读系统 / 清密钥环境 / rlimit）。容器内 bwrap 需用户命名空间能力——
# 若运行环境不放开（见 docker-compose 的 security_opt），沙箱探测不可用即 **fail-closed**
# （子进程类工具拒绝执行），绝不无沙箱裸跑（安全边界不退化）。

# ── 阶段 1：构建 React 前端（#0026）→ dist；阶段 2 复制进运行镜像由 FastAPI 静态托管 ──
FROM node:20-slim AS frontend
WORKDIR /fe
COPY coreagent/web/frontend/package.json coreagent/web/frontend/package-lock.json ./
RUN npm ci
COPY coreagent/web/frontend/ ./
RUN npm run build

# ── 阶段 2：运行镜像 ──
FROM python:3.11-slim

# 系统依赖：bubblewrap（沙箱）。ripgrep 二进制随包分发（coreagent/bin），无需 apt。
RUN apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装依赖（利用层缓存）：仅复制装包必需文件，再装 [web] extra（含 argon2-cffi / psycopg）。
COPY pyproject.toml ./
COPY coreagent ./coreagent
RUN pip install --no-cache-dir ".[web]"

# 前端构建产物（#0026）：从阶段 1 复制 dist，根路径 + /assets 由 web/app.py 静态托管。
COPY --from=frontend /fe/dist ./coreagent/web/frontend/dist

# 运行配置（compose 可覆盖）：监听全网卡、workspace / DB 落卷、Cookie 非 Secure（http 默认；
# 生产 https 置 COREAGENT_COOKIE_SECURE=1）。config.yaml（provider 密钥）由部署方挂载。
ENV COREAGENT_WEB_HOST=0.0.0.0 \
    COREAGENT_WEB_PORT=8000 \
    COREAGENT_WEB_WORKSPACE=/data/workspaces \
    COREAGENT_CONFIG=/app/config.yaml \
    COREAGENT_COOKIE_SECURE=0

EXPOSE 8000
CMD ["python", "-m", "coreagent.web"]
