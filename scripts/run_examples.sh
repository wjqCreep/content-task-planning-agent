#!/usr/bin/env bash
# 重新生成 examples/<mode>/ 下的运行记录。
# 用法（仓库根目录）：bash scripts/run_examples.sh real|mock
#   real 需要模型环境变量（见 .env.example）；mock 为离线流程验证。
set -u
MODE="${1:-mock}"
FLAG=""; [ "$MODE" = "mock" ] && FLAG="--mock"
export PYTHONPATH=src PYTHONIOENCODING=utf-8
BASE="examples/$MODE"
rm -rf "$BASE"; mkdir -p "$BASE"

# run <case名> <状态目录名> <参数...>；同一状态目录可被多个 case 复用（重复执行/续跑）
run() {
  local name="$1" state="$2"; shift 2
  local t0=$(date +%s)
  python -m content_agent "$@" $FLAG --out-dir "$BASE/state_$state" --trace-out "$BASE/$name.json" > /dev/null
  echo "$name exit=$? $(( $(date +%s) - t0 ))s"
}

run 01_normal_over_budget_replan  a --topic 咖啡 --budget 10 --deadline-hours 12
run 02_repeat_run                 a --topic 咖啡 --budget 10 --deadline-hours 12
run 03_missing_budget             b --topic 咖啡 --deadline-hours 12
run 04_tool_failure_replan        c --topic 咖啡 --budget 20 --deadline-hours 12 --inject-failure generate_script:premium:always
run 05_no_feasible_plan           d --topic 咖啡 --budget 5  --deadline-hours 12
run 06_save_fails_then_resume_1   e --topic 春节 --budget 20 --deadline-hours 12 --inject-failure save_plan:always
run 07_save_fails_then_resume_2   e --topic 春节 --budget 20 --deadline-hours 12
run 08_transient_error_retry      f --topic 开学 --budget 20 --deadline-hours 12 --inject-failure save_plan:once
run 09_step_limit                 g --topic 咖啡 --budget 10 --deadline-hours 12 --max-steps 3
run 10_no_materials               h --topic 不存在的主题 --budget 10 --deadline-hours 12
