"""业务工具：素材检索、脚本生成、成本估算、方案保存（本地函数 + 合成数据）。

费用只在工具成功时计入；失败调用不收费（见 docs/03-evaluation.md 的已知限制）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[2] / "data"
TIERS = ["premium", "standard", "lite"]  # 质量从高到低


class ToolError(Exception):
    def __init__(self, code: str, message: str, retriable: bool):
        super().__init__(message)
        self.code, self.message, self.retriable = code, message, retriable

    def to_dict(self) -> dict:
        return {"error": self.code, "message": self.message, "retriable": self.retriable}


class FailureInjector:
    """故障注入。规格：`tool[:tier]:once|always`。

    once   -> 该工具（可限定 tier）第一次调用抛可重试的超时错误
    always -> 每次调用都抛不可重试错误
    """

    def __init__(self, specs: list[str] | None = None):
        self.rules: list[dict] = []
        for s in specs or []:
            parts = s.split(":")
            if len(parts) == 2:
                tool, tier, mode = parts[0], None, parts[1]
            elif len(parts) == 3:
                tool, tier, mode = parts
            else:
                raise ValueError(f"bad failure spec: {s}")
            if mode not in ("once", "always"):
                raise ValueError(f"mode must be once|always: {s}")
            self.rules.append({"tool": tool, "tier": tier, "mode": mode, "fired": 0})

    def check(self, tool: str, tier: str | None = None) -> None:
        for r in self.rules:
            if r["tool"] != tool or (r["tier"] and r["tier"] != tier):
                continue
            if r["mode"] == "once" and r["fired"] == 0:
                r["fired"] += 1
                raise ToolError("timeout", f"{tool} 调用超时（注入的一次性故障）", True)
            if r["mode"] == "always":
                r["fired"] += 1
                raise ToolError("unavailable", f"{tool}{'/' + tier if tier else ''} 不可用（注入的持续故障）", False)


class ToolBox:
    def __init__(self, out_dir: Path, injector: FailureInjector | None = None):
        self.out_dir = Path(out_dir)
        self.injector = injector or FailureInjector()
        self.materials = json.loads((DATA_DIR / "materials.json").read_text(encoding="utf-8"))
        self.costs = json.loads((DATA_DIR / "tool_costs.json").read_text(encoding="utf-8"))

    # ---- 成本表查询（给 estimate 与 harness 守卫共用） ----
    def step_cost(self, tool: str, tier: str | None = None) -> tuple[float, float]:
        row = self.costs[tool][tier] if tier else self.costs[tool]
        return row["cost"], row["hours"]

    def plan_cost(self, tier: str) -> tuple[float, float]:
        c = h = 0.0
        for tool, t in (("search_materials", None), ("generate_script", tier), ("save_plan", None)):
            dc, dh = self.step_cost(tool, t)
            c += dc
            h += dh
        return c, h

    # ---- 工具 ----
    def search_materials(self, keywords: list[str]) -> dict:
        self.injector.check("search_materials")
        kws = [k.strip() for k in keywords if k and k.strip()]
        hits, excluded = [], 0
        for m in self.materials:
            if any(k in m["title"] or k in m["tags"] for k in kws):
                if m["license_ok"]:
                    hits.append({"id": m["id"], "title": m["title"], "type": m["type"]})
                else:
                    excluded += 1
        cost, hours = self.step_cost("search_materials")
        return {"materials": hits, "count": len(hits), "excluded_unlicensed": excluded,
                "cost": cost, "hours": hours}

    def generate_script(self, tier: str, material_ids: list[str]) -> dict:
        if tier not in TIERS:
            raise ToolError("bad_args", f"tier 必须是 {TIERS}", False)
        self.injector.check("generate_script", tier)
        titles = [m["title"] for m in self.materials if m["id"] in material_ids]
        desc = self.costs["generate_script"][tier]["desc"]
        script = {"tier": tier, "desc": desc, "outline": [f"开场：引入主题与素材《{t}》" for t in titles[:3]]
                  + ["中段：核心卖点与互动环节", "结尾：行动号召与转化入口"]}
        if tier == "premium":
            script["outline"] += ["备选版本 B：情景短剧版", "分镜表（12 镜）"]
        elif tier == "standard":
            script["outline"] += ["简要分镜（6 镜）"]
        cost, hours = self.step_cost("generate_script", tier)
        return {"script": script, "cost": cost, "hours": hours}

    def estimate_cost(self, tier: str) -> dict:
        """估算整套方案（检索+脚本+保存）的总费用/耗时；本身免费、不读写状态。"""
        self.injector.check("estimate_cost")
        if tier not in TIERS:
            raise ToolError("bad_args", f"tier 必须是 {TIERS}", False)
        c, h = self.plan_cost(tier)
        return {"tier": tier, "total_cost": c, "total_hours": h,
                "breakdown": {"search_materials": self.step_cost("search_materials")[0],
                              "generate_script": self.step_cost("generate_script", tier)[0],
                              "save_plan": self.step_cost("save_plan")[0]}}

    def save_plan(self, plan: dict, idem_key: str) -> dict:
        """幂等保存：同 idem_key 且内容相同则不重复写、不重复计费。"""
        self.injector.check("save_plan")
        self.out_dir.mkdir(parents=True, exist_ok=True)
        path = self.out_dir / f"plan_{idem_key}.json"
        body = json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True)
        if path.exists():
            if path.read_text(encoding="utf-8") == body:
                return {"path": str(path), "deduplicated": True, "cost": 0, "hours": 0}
            raise ToolError("conflict", "同一幂等键已存在内容不同的方案", False)
        path.write_text(body, encoding="utf-8")
        cost, hours = self.step_cost("save_plan")
        return {"path": str(path), "deduplicated": False, "cost": cost, "hours": hours}


def idem_key(topic: str, budget: float, deadline_hours: float) -> str:
    raw = f"{topic}|{budget}|{deadline_hours}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:10]
