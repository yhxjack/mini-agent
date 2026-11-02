#!/usr/bin/env bash
# 启动容器化 mini-agent(CLI,一次性任务)。
# 用法:
#   export MODEL_API_KEY=sk-xxxx
#   export OPENAI_BASE_URL=https://...   # 可选,OpenAI 兼容端点
#   export AGENT_MODEL=gpt-4o-mini       # 可选
#   ./run-agent.sh "在工作目录新建 hello.txt,写入当前目录文件列表"
set -euo pipefail

WORKSPACE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="${AGENT_IMAGE:-python:3.12-slim}"

: "${MODEL_API_KEY:?请先 export MODEL_API_KEY=<你的模型 API key>}"

docker run --rm -i \
  -v "$WORKSPACE":/workspace \
  -v mini_agent_pipcache:/root/.cache/pip \
  -w /workspace \
  -e MODEL_API_KEY="$MODEL_API_KEY" \
  -e OPENAI_BASE_URL="${OPENAI_BASE_URL:-}" \
  -e AGENT_MODEL="${AGENT_MODEL:-}" \
  -e HTTP_PROXY="${HTTP_PROXY:-}" \
  -e HTTPS_PROXY="${HTTPS_PROXY:-}" \
  -e NO_PROXY="${NO_PROXY:-}" \
  "$IMAGE" \
  bash -c 'pip install --quiet --disable-pip-version-check --root-user-action=ignore -r requirements.txt && python agent.py "$@"' _ "$@"
