"""CLI 入口：python -m content_agent --topic 咖啡 --budget 10 --deadline-hours 12 --mock"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import Agent
from .llm import AnthropicLLM, MockLLM
from .tools import FailureInjector, ToolBox


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="content_agent", description="内容任务规划 Agent")
    p.add_argument("--topic", default=None, help="活动主题（如 咖啡 / 开学 / 春节）")
    p.add_argument("--budget", type=float, default=None, help="预算（模拟单位）")
    p.add_argument("--deadline-hours", type=float, default=None, help="截止时间（小时）")
    p.add_argument("--mock", action="store_true", help="使用离线 MockLLM（只验证流程，不代表真实模型决策）")
    p.add_argument("--inject-failure", action="append", default=[], metavar="SPEC",
                   help="故障注入 tool[:tier]:once|always，如 generate_script:premium:always")
    p.add_argument("--out-dir", default="out", help="状态与方案保存目录")
    p.add_argument("--max-steps", type=int, default=12)
    p.add_argument("--max-tool-calls", type=int, default=10)
    p.add_argument("--fresh", action="store_true", help="忽略已有状态，重新开始")
    p.add_argument("--trace-out", default=None, help="把完整结果(含 trace)写入该 JSON 文件")
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        llm = MockLLM() if a.mock else AnthropicLLM()
    except RuntimeError as e:
        print(f"模型不可用：{e}\n可加 --mock 使用离线流程验证。", file=sys.stderr)
        return 2
    tb = ToolBox(Path(a.out_dir), FailureInjector(a.inject_failure))
    agent = Agent(llm, tb, Path(a.out_dir), a.max_steps, a.max_tool_calls)
    res = agent.run(a.topic, a.budget, a.deadline_hours, fresh=a.fresh)
    text = json.dumps(res, ensure_ascii=False, indent=2)
    if a.trace_out:
        Path(a.trace_out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.trace_out).write_text(text, encoding="utf-8")
    print(text)
    return 0 if res["status"] in ("completed", "already_completed") else 1
