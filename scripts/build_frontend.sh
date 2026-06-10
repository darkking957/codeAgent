#!/usr/bin/env bash
# 构建 #0026 React 前端产物到 coreagent/web/frontend/dist（供 FastAPI 静态托管）。
# 用法：scripts/build_frontend.sh
set -euo pipefail

FE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/coreagent/web/frontend"
cd "$FE_DIR"

echo "[build_frontend] npm ci @ $FE_DIR"
if [ -f package-lock.json ]; then
  npm ci
else
  npm install
fi

echo "[build_frontend] npm run build"
npm run build

echo "[build_frontend] done -> $FE_DIR/dist"
