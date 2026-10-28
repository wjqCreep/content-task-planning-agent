"""ReAct 循环：模型选动作 -> harness 校验并执行工具 -> 观察 -> 模型调整或结束。

harness 负责：依赖/预算/时限守卫、步数与工具调用上限、状态持久化、幂等与续跑。
模型负责：选哪个动作、选哪个档位、失败后重试/降级/停止。
"""
from __future__ import annotations

import json
from pathlib import Path

from .llm import SYSTEM_PROMPT, extract_json
from .tools import TIERS, ToolBox, ToolError, idem_key

STEP_NAMES = ["search_materials", "generate_script", "save_plan"]
ACTIONS = {"estimate_cost", *STEP_NAMES, "finish"}


def _new_steps() -> dict:
    return {n: {"status": "pending", "attempts": 0, "result": None, "error": None} for n in STEP_NAMES}


class Agent:
    def __init__(self, llm, toolbox: ToolBox, out_dir: Path, max_steps: int = 12, max_tool_calls: int = 10):
        self.llm, self.tb, self.out_dir = llm, toolbox, Path(out_dir)
        self.max_steps, self.max_tool_calls = max_steps, max_tool_calls

    # ------------------------------------------------------------ 状态
    def _state_path(self, key: str) -> Path:
        return self.out_dir / f"state_{key}.json"

    def _view(self, st: dict, step: int, tools_used: int) -> dict:
        """给模型看的状态摘要。"""
        return {
            "topic": st["topic"], "budget_total": st["budget"], "deadline_hours": st["deadline_hours"],
            "budget_remaining": round(st["budget"] - st["spent_cost"], 2),
            "hours_remaining": round(st["deadline_hours"] - st["spent_hours"], 2),
            "spent_cost": st["spent_cost"], "script_tier": st["script_tier"],
            "steps": {n: {"status": s["status"], "attempts": s["attempts"]} for n, s in st["steps"].items()},
            "step": step, "max_steps": self.max_steps, "tool_calls": tools_used, "max_tool_calls": self.max_tool_calls,
        }

    def _remaining_need(self, st: dict, tier: str) -> tuple[float, float]:
        c = h = 0.0
        for n in STEP_NAMES:
            if st["steps"][n]["status"] == "success":
                continue
            t = (st["script_tier"] or tier) if n == "generate_script" else None
            dc, dh = self.tb.step_cost(n, t)
            c, h = c + dc, h + dh
        return c, h

    # ------------------------------------------------------------ 入口
    def run(self, topic: str, budget, deadline_hours, fresh: bool = False) -> dict:
        missing = [k for k, v in (("budget", budget), ("deadline_hours", deadline_hours)) if v is None]
        if not topic or not topic.strip():
            missing.insert(0, "topic")
        if missing:
            return self._result(None, "needs_input", f"缺少必填输入：{', '.join(missing)}；未调用任何工具，也未产生费用", [],
                                extra={"missing": missing})
        if budget <= 0 or deadline_hours <= 0:
            return self._result(None, "needs_input", "budget 与 deadline_hours 必须为正数", [],
                                extra={"missing": ["budget" if budget <= 0 else "deadline_hours"]})

        key = idem_key(topic, budget, deadline_hours)
        sp = self._state_path(key)
        resumed = False
        if sp.exists() and not fresh:
            st = json.loads(sp.read_text(encoding="utf-8"))
            if st["status"] == "completed" and Path(st["saved_path"]).exists():
                return self._result(st, "already_completed", "相同输入已完成并保存，未重复执行、未重复计费", [],
                                    extra={"this_run_cost": 0})
            resumed = True
            if st["status"] == "completed":           # 状态说已完成但方案文件丢了：只重做保存
                st["steps"]["save_plan"].update(status="pending", result=None)
                st["saved_path"] = None
            for s in st["steps"].values():           # 保留成功结果，失败/不足的步骤重置为待执行
                if s["status"] != "success":
                    s.update(status="pending", error=None)
            st["status"] = "running"
        else:
            st = {"key": key, "topic": topic, "budget": budget, "deadline_hours": deadline_hours,
                  "spent_cost": 0.0, "spent_hours": 0.0, "script_tier": None, "materials": [],
                  "steps": _new_steps(), "status": "running", "saved_path": None, "runs": 0}
        st["runs"] += 1
        return self._loop(st, resumed)

    # ------------------------------------------------------------ 主循环
    def _loop(self, st: dict, resumed: bool) -> dict:
        trace: list[dict] = []
        tools_used = 0
        start_cost = st["spent_cost"]
        first = {"task": f"为主题「{st['topic']}」完成并保存内容活动方案，预算 {st['budget']} 模拟单位，截止 {st['deadline_hours']} 小时。",
                 "observation": None, "state": self._view(st, 0, 0)}
        messages = [{"role": "user", "content": json.dumps(first, ensure_ascii=False)}]
        final = None

        for step in range(1, self.max_steps + 1):
            try:
                raw = self.llm.next_action(SYSTEM_PROMPT, messages)
            except RuntimeError as e:
                final = ("incomplete", f"model_unavailable: {e}")
                break
            messages.append({"role": "assistant", "content": raw})
            act = extract_json(raw)
            if not act or act.get("action") not in ACTIONS or not isinstance(act.get("args", {}), dict):
                obs = {"action": "invalid", "error": "invalid_action", "retriable": True,
                       "message": f"输出必须是含 action(∈{sorted(ACTIONS)}) 的 JSON；收到：{raw[:80]!r}"}
                decision = {"action": "invalid", "args": {}, "reason": "(模型输出无法解析)"}
            else:
                decision = {"action": act["action"], "args": act.get("args", {}), "reason": str(act.get("reason", ""))[:300]}
                if decision["action"] == "finish":
                    ok, why = self._finish(st, decision["args"])
                    if ok:
                        trace.append(self._trace(step, decision, {"ok": True, "summary": "结束"}, st, tools_used))
                        final = (decision["args"]["status"] if decision["args"].get("status") in ("completed", "incomplete") else "incomplete",
                                 str(decision["args"].get("reason", ""))[:300])
                        break
                    obs = {"action": "finish", "error": "blocked_finish", "retriable": False, "message": why}
                elif tools_used >= self.max_tool_calls:
                    trace.append(self._trace(step, decision, {"ok": False, "summary": "已达工具调用上限，未执行"}, st, tools_used))
                    final = ("incomplete", f"tool_call_limit_reached: 已调用 {tools_used} 次工具")
                    break
                else:
                    tools_used += 1
                    obs = self._execute(st, decision["action"], decision["args"])
            summary = obs.get("error") and f"ERROR {obs['error']}: {obs.get('message','')}" or self._summ(obs)
            trace.append(self._trace(step, decision, {"ok": "error" not in obs, "summary": summary}, st, tools_used))
            self._save_state(st)
            messages.append({"role": "user", "content": json.dumps(
                {"observation": obs, "state": self._view(st, step, tools_used)}, ensure_ascii=False)})
        else:
            final = ("incomplete", f"step_limit_reached: 已用满 {self.max_steps} 步仍未结束")

        status, reason = final
        st["status"] = status
        self._save_state(st)
        extra = {"this_run_cost": round(st["spent_cost"] - start_cost, 2), "resumed": resumed}
        if status == "incomplete":
            extra["gap"] = self._gap(st)
        return self._result(st, status, reason, trace, extra=extra)

    # ------------------------------------------------------------ finish / 执行
    def _finish(self, st: dict, args: dict) -> tuple[bool, str]:
        status = args.get("status")
        if status not in ("completed", "incomplete"):
            return False, "finish.status 必须是 completed 或 incomplete"
        if status == "completed" and st["steps"]["save_plan"]["status"] != "success":
            return False, "save_plan 尚未成功，不能 finish completed（失败步骤不能记为成功）"
        return True, ""

    def _execute(self, st: dict, action: str, args: dict) -> dict:
        base = {"action": action, "args": args}
        stp = st["steps"].get(action)
        try:
            if action == "estimate_cost":
                tier = args.get("tier")
                est = self.tb.estimate_cost(tier)
                need_c, need_h = self._remaining_need(st, tier)
                rem_c, rem_h = st["budget"] - st["spent_cost"], st["deadline_hours"] - st["spent_hours"]
                return {**base, "tier": tier, **est, "remaining_cost_needed": need_c, "remaining_hours_needed": need_h,
                        "budget_remaining": round(rem_c, 2), "hours_remaining": round(rem_h, 2),
                        "affordable": need_c <= rem_c and need_h <= rem_h,
                        "cost_gap": round(max(0, need_c - rem_c), 2), "hours_gap": round(max(0, need_h - rem_h), 2)}

            if stp["status"] == "success":
                return {**base, "error": "already_done", "retriable": False,
                        "message": f"{action} 已成功，结果已保留，无需重复执行"}
            # 依赖守卫
            dep = {"generate_script": "search_materials", "save_plan": "generate_script"}.get(action)
            if dep and st["steps"][dep]["status"] != "success":
                return {**base, "error": "blocked_dependency", "retriable": False,
                        "message": f"{action} 依赖 {dep} 成功，当前状态 {st['steps'][dep]['status']}"}
            tier = args.get("tier") if action == "generate_script" else None
            if action == "generate_script" and tier not in TIERS:
                return {**base, "error": "bad_args", "retriable": False, "message": f"tier 必须是 {TIERS}"}
            # 预算/时限守卫：本步 + 后续未完成步骤 必须放得下
            need_c, need_h = self._remaining_need(st, tier or st["script_tier"] or "lite")
            if action == "generate_script":
                need_c, need_h = self._remaining_need({**st, "script_tier": tier}, tier)
            rem_c, rem_h = st["budget"] - st["spent_cost"], st["deadline_hours"] - st["spent_hours"]
            if need_c > rem_c or need_h > rem_h:
                return {**base, "tier": tier, "error": "blocked_budget", "retriable": False,
                        "message": f"完成剩余步骤需 {need_c} 单位/{need_h} 小时，余额 {round(rem_c, 2)} 单位/{round(rem_h, 2)} 小时",
                        "cost_gap": round(max(0, need_c - rem_c), 2), "hours_gap": round(max(0, need_h - rem_h), 2)}

            stp["attempts"] += 1
            if action == "search_materials":
                res = self.tb.search_materials(args.get("keywords") or [])
                self._charge(st, res)
                st["materials"] = res["materials"]
                if res["count"] == 0:
                    stp.update(status="insufficient", result=res, error="no_materials")
                    return {**base, "warning": "no_materials", "count": 0, "cost": res["cost"],
                            "message": "未检索到可用素材（已计费，步骤未成功）"}
                stp.update(status="success", result=res, error=None)
                return {**base, "count": res["count"], "ids": [m["id"] for m in res["materials"]],
                        "excluded_unlicensed": res["excluded_unlicensed"], "cost": res["cost"]}
            if action == "generate_script":
                res = self.tb.generate_script(tier, [m["id"] for m in st["materials"]])
                self._charge(st, res)
                st["script_tier"] = tier
                stp.update(status="success", result=res, error=None)
                return {**base, "tier": tier, "outline_items": len(res["script"]["outline"]), "cost": res["cost"]}
            if action == "save_plan":
                plan = self._plan_doc(st)
                res = self.tb.save_plan(plan, st["key"])
                self._charge(st, res)
                st["saved_path"] = res["path"]
                stp.update(status="success", result=res, error=None)
                return {**base, "path": res["path"], "cost": res["cost"]}
            return {**base, "error": "unknown_action", "retriable": False, "message": action}
        except ToolError as e:
            if stp is not None:
                stp.update(status="failed", error=e.to_dict())
            tier = args.get("tier")
            return {**base, "tier": tier, **e.to_dict()}

    def _charge(self, st: dict, res: dict) -> None:
        st["spent_cost"] = round(st["spent_cost"] + res["cost"], 2)
        st["spent_hours"] = round(st["spent_hours"] + res["hours"], 2)

    def _plan_doc(self, st: dict) -> dict:
        save_c, _ = self.tb.step_cost("save_plan")
        return {"topic": st["topic"], "budget": st["budget"], "deadline_hours": st["deadline_hours"],
                "tier": st["script_tier"], "materials": st["materials"],
                "script": st["steps"]["generate_script"]["result"]["script"],
                "cost_total": round(st["spent_cost"] + save_c, 2)}

    def _gap(self, st: dict) -> dict:
        c, h = self._remaining_need(st, "lite")
        rem_c, rem_h = st["budget"] - st["spent_cost"], st["deadline_hours"] - st["spent_hours"]
        return {"cheapest_remaining_cost": c, "budget_remaining": round(rem_c, 2), "cost_shortfall": round(max(0, c - rem_c), 2),
                "cheapest_remaining_hours": h, "hours_remaining": round(rem_h, 2), "hours_shortfall": round(max(0, h - rem_h), 2)}

    # ------------------------------------------------------------ 输出
    @staticmethod
    def _summ(obs: dict) -> str:
        keys = [k for k in obs if k not in ("action", "args")]
        return ", ".join(f"{k}={obs[k]}" for k in keys)[:240]

    @staticmethod
    def _trace(step, decision, observation, st, tools_used) -> dict:
        return {"step": step, "decision": decision, "observation": observation,
                "state_after": {"spent_cost": st["spent_cost"], "spent_hours": st["spent_hours"],
                                "steps": {n: s["status"] for n, s in st["steps"].items()}, "tool_calls": tools_used}}

    def _save_state(self, st: dict) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._state_path(st["key"]).write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")

    def _result(self, st, status, reason, trace, extra=None) -> dict:
        res = {"llm": getattr(self.llm, "name", "?"), "status": status, "reason": reason,
               "limits": {"max_steps": self.max_steps, "max_tool_calls": self.max_tool_calls}}
        if st:
            res.update(topic=st["topic"], budget=st["budget"], deadline_hours=st["deadline_hours"],
                       cost_total=st["spent_cost"], hours_total=st["spent_hours"], script_tier=st["script_tier"],
                       saved_path=st["saved_path"] if status in ("completed", "already_completed") else None,
                       plan_steps={n: {"status": s["status"], "attempts": s["attempts"], "error": s["error"]}
                                   for n, s in st["steps"].items()})
        res.update(extra or {})
        res["trace"] = trace
        return res
