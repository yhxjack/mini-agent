#!/usr/bin/env python3
"""Mini-agent 共享核心:模型客户端、工具、分发、agent loop(非流式 + 流式)。
CLI(agent.py)与 Web(server.py)共用。
"""
import json
import os
import subprocess
import sys

from openai import OpenAI

BASE_URL = os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
MODEL = os.environ.get("AGENT_MODEL") or "gpt-4o-mini"
WORKSPACE = "/workspace"
MAX_STEPS = 25
MAX_OUTPUT = 10_000   # 工具输出截断上限(字符)
CMD_TIMEOUT = 120     # run_command 超时(秒)

SYSTEM_PROMPT = (
    "你是一个运行在容器内的编码 agent,工作目录是 /workspace。"
    "你可以读写文件、列目录、执行 shell 命令来完成用户任务。"
    "需要信息时主动调用工具;任务完成后直接用自然语言给出最终答复,不要再调用工具。"
)


def make_client():
    key = os.environ.get("MODEL_API_KEY")
    if not key:
        sys.exit("ERROR: 环境变量 MODEL_API_KEY 未设置")
    return OpenAI(base_url=BASE_URL, api_key=key)


class ExecEnv:
    """工具执行环境:工作目录 + 降权 OS uid(None 表示不降权,如 CLI)。"""
    def __init__(self, workdir=WORKSPACE, os_uid=None):
        self.workdir = workdir
        self.os_uid = os_uid


DEFAULT_ENV = ExecEnv()


# ---------- 工具实现(均接收 env) ----------
def _resolve(path, base):
    # 安全收敛:文件操作强制落在 base(用户专属工作目录)内,越界抛错
    path = str(path)
    p = os.path.normpath(path if os.path.isabs(path) else os.path.join(base, path))
    if p != base and not p.startswith(base + os.sep):
        raise ValueError(f"路径越界,仅允许工作目录内: {path}")
    return p


def tool_read_file(path, env):
    p = _resolve(path, env.workdir)
    if not os.path.isfile(p):
        return f"ERROR: 文件不存在: {path}"
    with open(p, "r", encoding="utf-8", errors="replace") as f:
        return f.read()[:MAX_OUTPUT]


def tool_write_file(path, content, env):
    p = _resolve(path, env.workdir)
    os.makedirs(os.path.dirname(p) or env.workdir, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    if env.os_uid:
        os.chown(p, env.os_uid, env.os_uid)   # 让降权命令可访问自己的文件
    return f"OK: 已写入 {path} ({len(content)} 字符)"
# <!-- C1 -->


def tool_list_dir(path, env):
    p = _resolve(path, env.workdir)
    if not os.path.isdir(p):
        return f"ERROR: 目录不存在: {path}"
    return "\n".join(sorted(os.listdir(p))) or "(空目录)"


def tool_run_command(command, env):
    def _drop():
        if env.os_uid:
            os.setgid(env.os_uid)
            os.setuid(env.os_uid)
    try:
        r = subprocess.run(command, shell=True, cwd=env.workdir,
                           capture_output=True, text=True, timeout=CMD_TIMEOUT,
                           preexec_fn=_drop if env.os_uid else None)
    except subprocess.TimeoutExpired:
        return f"ERROR: 命令超时(>{CMD_TIMEOUT}s)"
    out = (r.stdout or "") + (r.stderr or "")
    return f"[exit {r.returncode}]\n{out[:MAX_OUTPUT]}"


# ---------- 工具 schema 与分发 ----------
TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": "读取 /workspace 下文件内容",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "写入文件(覆盖),自动建父目录",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"},
                                      "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
    {"type": "function", "function": {
        "name": "list_dir", "description": "列出目录下条目",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "run_command", "description": "在 /workspace 执行 shell 命令",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]}}},
]


def dispatch_tool(name, args, env):
    try:
        if name == "read_file":
            return tool_read_file(args["path"], env)
        if name == "write_file":
            return tool_write_file(args["path"], args["content"], env)
        if name == "list_dir":
            return tool_list_dir(args["path"], env)
        if name == "run_command":
            return tool_run_command(args["command"], env)
        return f"ERROR: unknown tool {name}"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"
# <!-- C2 -->


# ---------- 非流式 loop(CLI 用)----------
def run_agent_loop(client, task, max_steps=MAX_STEPS, env=DEFAULT_ENV):
    messages = [{"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": task}]
    for step in range(1, max_steps + 1):
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS,
            tool_choice="auto", max_tokens=4096)
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            print(f"\n=== 最终答复 ===\n{msg.content}")
            return
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            print(f"[step {step}] 工具 {tc.function.name} {args}")
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": dispatch_tool(tc.function.name, args, env)})
    print("[已达最大步数上限,终止]")


# ---------- 流式 loop(Web 用,yield 事件 dict)----------
def stream_agent_turn(client, messages, max_steps=MAX_STEPS, approver=None,
                      env=DEFAULT_ENV):
    for _ in range(max_steps):
        stream = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS,
            tool_choice="auto", max_tokens=4096, stream=True)
        content, calls = [], {}        # calls: index -> {id,name,args}
        for ck in stream:
            if not ck.choices:
                continue
            d = ck.choices[0].delta
            if d.content:
                content.append(d.content)
                yield {"type": "token", "data": d.content}
            for tcd in (d.tool_calls or []):
                a = calls.setdefault(tcd.index, {"id": "", "name": "", "args": ""})
                if tcd.id:
                    a["id"] = tcd.id
                if tcd.function and tcd.function.name:
                    a["name"] = tcd.function.name
                if tcd.function and tcd.function.arguments:
                    a["args"] += tcd.function.arguments
        msg = {"role": "assistant", "content": "".join(content)}
        if calls:
            msg["tool_calls"] = [{"id": a["id"], "type": "function",
                "function": {"name": a["name"], "arguments": a["args"]}}
                for a in calls.values()]
        messages.append(msg)
        if not calls:
            yield {"type": "final", "data": msg["content"]}
            return
        for a in calls.values():
            args = json.loads(a["args"] or "{}")
            yield {"type": "tool_call", "data": {"name": a["name"], "args": args}}
            if approver and approver.needs(a["name"], args):
                rid = approver.open(a["name"], args)
                yield {"type": "approval_request",
                       "data": {"request_id": rid, "name": a["name"], "args": args}}
                if not approver.wait(rid):
                    result = "ERROR: 用户拒绝执行该操作"
                    yield {"type": "tool_result",
                           "data": {"name": a["name"], "result": result}}
                    messages.append({"role": "tool", "tool_call_id": a["id"],
                                     "content": result})
                    continue
            result = dispatch_tool(a["name"], args, env)
            yield {"type": "tool_result", "data": {"name": a["name"], "result": result}}
            messages.append({"role": "tool", "tool_call_id": a["id"],
                             "content": result})
    yield {"type": "final", "data": "[已达最大步数]"}


