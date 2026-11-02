#!/usr/bin/env bash
# 启动容器化 Agent-Web 服务(FastAPI + SSE 流式 + 单页 UI)。
# 用法:
#   export MODEL_API_KEY=sk-xxxx                 # 必填:模型 API key
#   export OPENAI_BASE_URL=https://...           # 可选:OpenAI 兼容端点(默认 https://api.openai.com/v1)
#   export AGENT_MODEL=gpt-4o-mini               # 可选:主模型
#   export HTTP_PROXY=http://host:port           # 可选:如需代理访问外网
#   ./run-web.sh
# 启动后浏览器访问 http://localhost:8080(远程开发环境需做端口转发)
set -euo pipefail

WORKSPACE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${AGENT_IMAGE:-python:3.12-slim}"
PORT="${PORT:-8080}"

: "${MODEL_API_KEY:?请先 export MODEL_API_KEY=<你的模型 API key>}"

docker run --rm \
  --name agent-web \
  -p "${PORT}:8080" \
  -v "$WORKSPACE":/workspace \
  -v mini_agent_pipcache:/root/.cache/pip \
  -w /workspace \
  -e MODEL_API_KEY="$MODEL_API_KEY" \
  -e OPENAI_BASE_URL="${OPENAI_BASE_URL:-}" \
  -e AGENT_MODEL="${AGENT_MODEL:-}" \
  -e TITLE_MODEL="${TITLE_MODEL:-}" \
  -e ADMIN_PASSWORD="${ADMIN_PASSWORD:-}" \
  -e HTTP_PROXY="${HTTP_PROXY:-}" \
  -e HTTPS_PROXY="${HTTPS_PROXY:-}" \
  -e NO_PROXY="${NO_PROXY:-}" \
  "$IMAGE" \
  bash -c 'pip install --quiet --disable-pip-version-check --root-user-action=ignore -r requirements.txt && exec uvicorn server:app --host 0.0.0.0 --port 8080'
