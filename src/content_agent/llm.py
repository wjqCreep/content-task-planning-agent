"""模型接入：真实模型（Anthropic Messages 兼容接口，仅标准库）与离线 Mock。

Mock 只用于验证流程/守卫/状态，不证明真实模型的决策效果。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from .tools import TIERS

SYSTEM_PROMPT = """你是内容活动方案的规划 Agent。目标：在预算(模拟单位)与截止时间(小时)内，为运营人员完成并保存一份内容活动方案。
每一轮你只输出一个 JSON 对象（不要 markdown、不要多余文字），格式：
{"action": "<动作名>", "args": {...}, "reason": "<一句话决策依据，引用你观察到的数字或错误>"}

可用动作：
- estimate_cost  args={"tier": "premium|standard|lite"}  估算整套方案(检索+脚本+保存)费用与耗时，免费；返回里有按当前已花费算出的 remaining_cost_needed 和 affordable。
- search_materials  args={"keywords": ["..."]}  检索素材（关键词由你根据主题自行拟定；返回 count=0 表示资料不足）。
- generate_script  args={"tier": "premium|standard|lite"}  生成脚本（依赖素材检索成功）。
- save_plan  args={}  保存方案（依赖脚本生成成功）。
- finish  args={"status": "completed|incomplete", "reason": "..."}  结束。

规则：
1. 先用 estimate_cost 判断档位是否可行，再执行工具；不要执行明知超预算的工具。
2. 观察到超预算时改选更便宜的档位并重新估算；观察到可重试错误(retriable=true)可重试一次，不可重试(retriable=false)就换档位或停止。
3. 已成功的步骤结果会保留，不要重复执行已成功的步骤。
4. 没有任何可行方案时，用 finish 返回 incomplete 并说明缺口；只有 save_plan 成功后才能 finish completed。
5. 步数有限，不要无意义地重复调用。
6. 素材检索结果为空(资料不足)时，不要编造素材或擅自换成与主题无关的关键词凑数；换一次与主题相关的关键词仍为空，就 finish incomplete 并说明缺少什么。"""


class AnthropicLLM:
    name = "anthropic"

    def __init__(self, model: str | None = None, base_url: str | None = None, token: str | None = None,
                 timeout: float = 90):
        self.model = model or os.environ.get("CONTENT_AGENT_MODEL") or os.environ.get("ANTHROPIC_DEFAULT_SONNET_MODEL")
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com").rstrip("/")
        self.token = token or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY")
        self.timeout = timeout
        if not self.model or not self.token:
            raise RuntimeError("缺少模型配置：需要 CONTENT_AGENT_MODEL(或 ANTHROPIC_DEFAULT_SONNET_MODEL) 与 ANTHROPIC_AUTH_TOKEN")

    def next_action(self, system: str, messages: list[dict]) -> str:
        body = json.dumps({"model": self.model, "max_tokens": 600,
                           "system": system, "messages": messages}).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/v1/messages", data=body, method="POST",
            headers={"content-type": "application/json", "anthropic-version": "2023-06-01",
                     "authorization": f"Bearer {self.token}", "x-api-key": self.token})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"模型接口 HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:200]}") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"模型接口不可达: {e.reason}") from None
        return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


class MockLLM:
    """离线策略：读取与真实模型相同的观察 JSON，输出同格式动作。

    它是写死的规则，仅用于离线验证 harness（守卫、状态、重规划接线），不代表模型决策能力。
    """
    name = "mock"

    def __init__(self):
        self.tier_idx = 0           # 当前尝试的档位下标
        self.retried: set = set()   # 已重试过的 (tool, tier)
        self.search_done = False
        self.script_done = False

    def _act(self, action, why, **args):
        return json.dumps({"action": action, "args": args, "reason": why}, ensure_ascii=False)

    def _lower_or_stop(self, why: str) -> str:
        if self.tier_idx + 1 < len(TIERS):
            self.tier_idx += 1
            return self._act("estimate_cost", f"{why}；改估更便宜的档位 {TIERS[self.tier_idx]}", tier=TIERS[self.tier_idx])
        return self._act("finish", f"{why}；已无更低档位可选", status="incomplete",
                         reason=f"{why}；所有档位均不可行")

    def next_action(self, system: str, messages: list[dict]) -> str:
        msg = json.loads(messages[-1]["content"])
        obs, state = msg.get("observation"), msg["state"]
        tier = TIERS[self.tier_idx]
        steps = state["steps"]
        if obs is None:
            return self._act("estimate_cost", "先估算最高档位是否在预算与时限内", tier=tier)
        kind = obs.get("action")
        if "error" in obs:
            code, retriable = obs["error"], obs.get("retriable", False)
            key = (kind, obs.get("tier") or obs.get("args", {}).get("tier"))
            if code == "blocked_budget":
                return self._lower_or_stop(f"{kind} 被预算/时限守卫拦截")
            if code == "blocked_dependency":
                return self._act("finish", f"{kind} 的前置步骤未成功", status="incomplete",
                                 reason=f"{kind} 被依赖守卫拦截: {obs.get('message', '')}")
            if retriable and key not in self.retried:
                self.retried.add(key)
                return self._act(kind, f"{kind} 返回可重试错误 {code}，重试一次", **obs.get("args", {}))
            if kind == "generate_script":
                return self._lower_or_stop(f"{tier} 脚本生成失败({code})")
            return self._act("finish", f"{kind} 失败({code})且不可恢复", status="incomplete",
                             reason=f"{kind} 失败({code}): {obs.get('message', '')}")
        if kind == "estimate_cost":
            if obs["affordable"]:
                why = f"{tier} 可行(剩余需 {obs['remaining_cost_needed']} ≤ 预算余额 {state['budget_remaining']})"
                if steps["search_materials"]["status"] != "success":
                    return self._act("search_materials", f"{why}，先检索素材", keywords=[state["topic"]])
                if steps["generate_script"]["status"] != "success":
                    return self._act("generate_script", f"{why}，素材已就绪", tier=tier)
                return self._act("save_plan", f"{why}，检索与脚本结果已保留，只续跑保存")
            return self._lower_or_stop(f"{tier} 估算 {obs['remaining_cost_needed']} 超过预算余额 {state['budget_remaining']}")
        if kind == "search_materials":
            if obs.get("warning") == "no_materials":
                return self._act("finish", "检索无可用素材，资料不足", status="incomplete",
                                 reason="素材检索未命中任何可用素材，请补充主题关键词或素材")
            return self._act("generate_script", "素材检索成功，按已选档位生成脚本", tier=tier)
        if kind == "generate_script":
            return self._act("save_plan", "脚本已生成，保存方案")
        if kind == "save_plan":
            return self._act("finish", "方案已保存", status="completed", reason="全部步骤成功")
        return self._act("finish", "无法理解观察", status="incomplete", reason="mock 无法处理该观察")


def extract_json(text: str) -> dict | None:
    text = text.strip()
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None
