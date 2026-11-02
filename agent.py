#!/usr/bin/env python3
"""Mini-Agent CLI 入口。核心逻辑在 agent_core.py(CLI 与 Web 共用)。"""
import sys

from agent_core import make_client, run_agent_loop


def main():
    task = " ".join(sys.argv[1:]).strip()
    if not task:
        task = input("输入任务> ").strip()
    if not task:
        sys.exit("ERROR: 任务为空")
    run_agent_loop(make_client(), task)


if __name__ == "__main__":
    main()
