"""离线测试（标准库 unittest，使用 MockLLM 与脚本化 LLM，不访问网络）。
运行：python -m unittest discover -s tests -v   （仓库根目录，需 PYTHONPATH=src）
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from content_agent.agent import Agent  # noqa: E402
from content_agent.llm import MockLLM  # noqa: E402
from content_agent.tools import FailureInjector, ToolBox  # noqa: E402


class ScriptedLLM:
    """按预设动作序列回放，用来单独测试 harness 守卫（模型“犯错”时 harness 是否拦得住）。"""
    name = "scripted"

    def __init__(self, actions):
        self.actions = list(actions)

    def next_action(self, system, messages):
        a = self.actions.pop(0)
        return a if isinstance(a, str) else json.dumps(a, ensure_ascii=False)


def act(action, **args):
    return {"action": action, "args": args, "reason": "test"}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def agent(self, llm=None, failures=None, **kw):
        tb = ToolBox(self.dir, FailureInjector(failures))
        return Agent(llm or MockLLM(), tb, self.dir, **kw)


class TestCore(Base):
    def test_over_budget_replan_to_cheaper_tier(self):
        r = self.agent().run("咖啡", 10, 12)
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["script_tier"], "standard")
        self.assertEqual(r["cost_total"], 8.0)
        est = [t["decision"]["args"]["tier"] for t in r["trace"] if t["decision"]["action"] == "estimate_cost"]
        self.assertEqual(est, ["premium", "standard"])
        self.assertTrue(Path(r["saved_path"]).exists())

    def test_missing_budget_or_deadline_runs_no_tools(self):
        for kw in ({"budget": None, "deadline_hours": 12}, {"budget": 10, "deadline_hours": None}):
            r = self.agent().run("咖啡", **kw)
            self.assertEqual(r["status"], "needs_input")
            self.assertEqual(r["trace"], [])
            self.assertEqual(list(self.dir.iterdir()), [])  # 无状态、无方案产生

    def test_no_feasible_plan_reports_gap(self):
        r = self.agent().run("咖啡", 5, 12)
        self.assertEqual(r["status"], "incomplete")
        self.assertEqual(r["cost_total"], 0)
        self.assertEqual(r["gap"]["cost_shortfall"], 1.0)
        self.assertIsNone(r["saved_path"])

    def test_deadline_gap(self):
        r = self.agent().run("咖啡", 100, 2)  # lite 需要 2.5 小时
        self.assertEqual(r["status"], "incomplete")
        self.assertEqual(r["gap"]["hours_shortfall"], 0.5)


class TestFailures(Base):
    def test_tool_failure_keeps_success_and_replans(self):
        r = self.agent(failures=["generate_script:premium:always"]).run("咖啡", 20, 12)
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["script_tier"], "standard")
        self.assertEqual(r["plan_steps"]["search_materials"]["attempts"], 1)  # 检索结果被保留，没有重跑
        self.assertEqual(r["plan_steps"]["generate_script"]["attempts"], 2)
        self.assertEqual(r["cost_total"], 8.0)  # 失败调用不计费
        actions = [t["decision"]["action"] for t in r["trace"]]
        self.assertEqual(actions.count("search_materials"), 1)

    def test_failed_step_never_recorded_as_success(self):
        r = self.agent(failures=["save_plan:always"]).run("春节", 20, 12)
        self.assertEqual(r["status"], "incomplete")
        self.assertEqual(r["plan_steps"]["save_plan"]["status"], "failed")
        self.assertIsNone(r["saved_path"])
        self.assertEqual(list(self.dir.glob("plan_*.json")), [])

    def test_transient_error_retried_once(self):
        r = self.agent(failures=["save_plan:once"]).run("开学", 20, 12)
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["plan_steps"]["save_plan"]["attempts"], 2)

    def test_resume_only_reruns_failed_step(self):
        r1 = self.agent(failures=["save_plan:always"]).run("春节", 20, 12)
        self.assertEqual(r1["cost_total"], 10.0)
        r2 = self.agent().run("春节", 20, 12)  # 故障消除后同输入再跑
        self.assertEqual(r2["status"], "completed")
        self.assertTrue(r2["resumed"])
        self.assertEqual(r2["this_run_cost"], 2.0)  # 只付保存的钱
        self.assertEqual(r2["cost_total"], 12.0)
        self.assertEqual(r2["plan_steps"]["search_materials"]["attempts"], 1)
        self.assertEqual(r2["plan_steps"]["generate_script"]["attempts"], 1)

    def test_no_materials_is_insufficient_not_success(self):
        r = self.agent().run("不存在的主题", 10, 12)
        self.assertEqual(r["status"], "incomplete")
        self.assertEqual(r["plan_steps"]["search_materials"]["status"], "insufficient")
        self.assertIsNone(r["saved_path"])


class TestRepeat(Base):
    def test_repeat_completed_is_idempotent(self):
        r1 = self.agent().run("咖啡", 10, 12)
        r2 = self.agent().run("咖啡", 10, 12)
        self.assertEqual(r2["status"], "already_completed")
        self.assertEqual(r2["this_run_cost"], 0)
        self.assertEqual(r2["trace"], [])
        self.assertEqual(r2["saved_path"], r1["saved_path"])
        self.assertEqual(len(list(self.dir.glob("plan_*.json"))), 1)

    def test_completed_state_but_plan_file_missing_resaves_only(self):
        r1 = self.agent().run("咖啡", 10, 12)
        Path(r1["saved_path"]).unlink()
        r2 = self.agent().run("咖啡", 10, 12)
        self.assertEqual(r2["status"], "completed")
        self.assertTrue(Path(r2["saved_path"]).exists())
        self.assertEqual(r2["plan_steps"]["search_materials"]["attempts"], 1)
        self.assertEqual(r2["plan_steps"]["save_plan"]["attempts"], 2)

    def test_fresh_rerun_does_not_duplicate_or_double_charge_file(self):
        self.agent().run("咖啡", 10, 12)
        r = self.agent().run("咖啡", 10, 12, fresh=True)
        self.assertEqual(r["status"], "completed")
        self.assertEqual(len(list(self.dir.glob("plan_*.json"))), 1)


class TestLimitsAndGuards(Base):
    def test_step_limit(self):
        r = self.agent(max_steps=3).run("咖啡", 10, 12)
        self.assertEqual(r["status"], "incomplete")
        self.assertIn("step_limit_reached", r["reason"])

    def test_tool_call_limit(self):
        r = self.agent(max_tool_calls=2).run("咖啡", 10, 12)
        self.assertEqual(r["status"], "incomplete")
        self.assertIn("tool_call_limit_reached", r["reason"])
        self.assertEqual(r["trace"][-1]["observation"]["ok"], False)

    def test_dependency_guard(self):
        llm = ScriptedLLM([act("save_plan"), act("finish", status="incomplete", reason="x")])
        r = self.agent(llm).run("咖啡", 10, 12)
        self.assertIn("blocked_dependency", r["trace"][0]["observation"]["summary"])
        self.assertEqual(r["plan_steps"]["save_plan"]["attempts"], 0)

    def test_budget_guard_blocks_overspend_even_if_model_insists(self):
        llm = ScriptedLLM([act("search_materials", keywords=["咖啡"]), act("generate_script", tier="premium"),
                           act("finish", status="incomplete", reason="x")])
        r = self.agent(llm).run("咖啡", 10, 12)
        self.assertIn("blocked_budget", r["trace"][1]["observation"]["summary"])
        self.assertEqual(r["cost_total"], 2.0)  # 只有检索花了钱
        self.assertEqual(r["plan_steps"]["generate_script"]["attempts"], 0)

    def test_cannot_finish_completed_without_saving(self):
        llm = ScriptedLLM([act("finish", status="completed", reason="骗你的"),
                           act("finish", status="incomplete", reason="ok")])
        r = self.agent(llm).run("咖啡", 10, 12)
        self.assertIn("blocked_finish", r["trace"][0]["observation"]["summary"])
        self.assertEqual(r["status"], "incomplete")

    def test_invalid_model_output_is_observed_not_crash(self):
        llm = ScriptedLLM(["我觉得先聊聊", act("finish", status="incomplete", reason="x")])
        r = self.agent(llm).run("咖啡", 10, 12)
        self.assertIn("invalid_action", r["trace"][0]["observation"]["summary"])

    def test_repeating_successful_step_is_refused(self):
        llm = ScriptedLLM([act("search_materials", keywords=["咖啡"]), act("search_materials", keywords=["咖啡"]),
                           act("finish", status="incomplete", reason="x")])
        r = self.agent(llm).run("咖啡", 10, 12)
        self.assertIn("already_done", r["trace"][1]["observation"]["summary"])
        self.assertEqual(r["cost_total"], 2.0)  # 没有第二次扣费


class TestInjector(unittest.TestCase):
    def test_bad_spec(self):
        with self.assertRaises(ValueError):
            FailureInjector(["generate_script:sometimes"])


if __name__ == "__main__":
    unittest.main()
