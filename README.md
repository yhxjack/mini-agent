# Mini-Agent

> 一个从零手搓的轻量级智能体(Agent)框架:`Agent = Model + Harness`。核心循环极简,复杂度交给模型;附带 Web UI、多轮会话/工作区、命令审批,以及账号鉴权 + 多租户隔离。

## 特性

- **极简 Agent Loop**:`while(true){ 请求模型 → 解析工具调用 → 执行工具 → 回填结果 }`,原生 Function Calling。
- **内置工具**:`read_file` / `write_file` / `list_dir` / `run_command`。
- **Web UI**:FastAPI + SSE 流式输出,工具调用过程可视化;DSH 式「工作区 → 会话」管理,多轮对话、刷新恢复、会话标题自动生成。
- **CLI**:`run-agent.sh` 一次性跑单个任务。
- **安全**:文件操作路径收敛;危险命令人工审批,只读安全命令自动放行。
- **多租户**:账号密码登录(pbkdf2 哈希)+ SQLite 持久化 + 按用户数据隔离 + 每用户 OS 降权执行隔离。
- **OpenAI 兼容**:任意 OpenAI 兼容端点皆可接入(通过环境变量配置)。

## 架构

```
浏览器(单页 UI, EventSource)
  └─ FastAPI(:8080)
       ├─ 鉴权(SQLite:users/tokens)
       ├─ 数据(SQLite:workspaces/sessions,按 uid 隔离)
       ├─ Agent Loop(stream_agent_turn,SSE)
       │     ├─ 模型客户端(OpenAI 兼容)
       │     ├─ 工具集 + 审批钩子
       │     └─ 执行环境 ExecEnv:每用户专属目录 + OS uid 降权
       └─ 单页 index.html
```

<!-- R -->
## 快速开始

依赖:**Docker**(推荐,自带隔离)。容器镜像 `python:3.12-slim`。

```bash
# 1. 配置模型(必填 key;其余可选,默认走 OpenAI 官方端点)
export MODEL_API_KEY=sk-xxxx
export OPENAI_BASE_URL=https://api.openai.com/v1   # 可选:任意 OpenAI 兼容端点
export AGENT_MODEL=gpt-4o-mini                     # 可选:主模型
# export HTTP_PROXY=http://host:port               # 可选:需代理访问外网时

# 2. 启动 Web 服务(http://localhost:8080)
./run-web.sh

# 或:CLI 跑一次性任务
./run-agent.sh "列出当前目录并统计有多少个 .py 文件"
```

首次打开 Web 用内置管理员登录,或自助注册:

- 默认管理员:`admin` / `admin@123` —— **生产务必修改**(启动前 `export ADMIN_PASSWORD=<强密码>`)。

> 远程/无头服务器:请用 IDE 或 `ssh -L` 做端口转发后再从本地浏览器访问 8080。

## 环境变量

| 变量 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `MODEL_API_KEY` | 是 | — | 模型 API key |
| `OPENAI_BASE_URL` | 否 | `https://api.openai.com/v1` | OpenAI 兼容端点 |
| `AGENT_MODEL` | 否 | `gpt-4o-mini` | 主模型 |
| `TITLE_MODEL` | 否 | 同 `AGENT_MODEL` | 生成会话标题的模型 |
| `ADMIN_USER` / `ADMIN_PASSWORD` | 否 | `admin` / `admin@123` | 内置管理员 |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY` | 否 | — | 外网代理 |

## 安全提示

- 本项目为**原型/学习性质**,未经安全审计,请勿直接用于生产。
- `run_command` 在容器内执行,危险命令需人工审批;每用户以独立 OS uid 降权、专属 0700 目录,跨用户无法互读文件。但同容器同内核,世界可读系统文件仍可读;更强隔离请每用户独立容器/VM。
- 默认 admin 密码仅便于首登,务必修改。
- 服务默认 http、无 CSRF/限流,对外暴露前请自行加 HTTPS、鉴权网关与速率限制。

## 目录

| 文件 | 说明 |
|---|---|
| `agent_core.py` | 模型客户端、工具、Agent Loop(CLI + 流式)、执行环境 |
| `server.py` | FastAPI:鉴权、工作区/会话、SSE、审批、租户隔离 |
| `index.html` | 单页 Web UI |
| `agent.py` / `run-agent.sh` | CLI 入口 |
| `run-web.sh` | Web 服务容器启动脚本 |

## License

MIT
