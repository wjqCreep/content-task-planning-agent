# 内容任务规划 Agent

运营人员给出主题、预算（模拟单位）、截止时间（小时），Agent 用「模型选动作 → 执行工具 → 观察结果 → 模型调整或结束」的 ReAct 循环，在预算内完成并保存一份内容活动方案。预算超限时降档重估，工具失败时保留已成功步骤并调整，没有可行方案时返回未完成及缺口。

## 环境
- Python 3.10+（开发环境 3.11.9），无第三方依赖，见 [requirements.txt](requirements.txt)。
- 真实模型：Anthropic Messages 兼容接口，变量见 [.env.example](.env.example)。没有模型时用 `--mock`。
- 未使用 Agent 框架：循环、守卫、状态都是原生实现（取舍见 [docs/02-solution.md](docs/02-solution.md)）。

## 运行（仓库根目录）
bash / Git Bash：
```bash
export PYTHONPATH=src PYTHONIOENCODING=utf-8
# 离线 Mock（只验证流程，不代表真实模型决策）
python -m content_agent --topic 咖啡 --budget 10 --deadline-hours 12 --mock --out-dir out
# 真实模型（需先设置 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN / CONTENT_AGENT_MODEL）
python -m content_agent --topic 咖啡 --budget 10 --deadline-hours 12 --out-dir out
```
PowerShell：
```powershell
$env:PYTHONPATH="src"; $env:PYTHONIOENCODING="utf-8"
python -m content_agent --topic 咖啡 --budget 10 --deadline-hours 12 --mock --out-dir out
```
常用参数：`--inject-failure generate_script:premium:always`（故障注入，格式 `工具[:档位]:once|always`）、`--max-steps`、`--max-tool-calls`、`--fresh`（忽略已有状态）、`--trace-out FILE`。
退出码：completed / already_completed 为 0，其余（含 needs_input、incomplete）为 1，模型未配置为 2。

可用主题关键词（合成素材）：咖啡、开学、春节、门店/开业。

## 测试
```bash
PYTHONPATH=src python -m unittest discover -s tests -v    # 20 个离线测试，不访问网络
bash scripts/run_examples.sh mock                         # 重新生成 examples/mock/
bash scripts/run_examples.sh real                         # 重新生成 examples/real/（调真实模型，约 3 分钟）
```

## 评审入口
1. [docs/03-evaluation.md](docs/03-evaluation.md)：验收点 ↔ 用例 ↔ 运行记录对照、失败与限制。
2. 重规划看 [examples/real/01_normal_over_budget_replan.json](examples/real/01_normal_over_budget_replan.json)（超预算降档）和 [examples/real/04_tool_failure_replan.json](examples/real/04_tool_failure_replan.json)（注入失败后降档、保留检索结果）。
3. 代码：[src/content_agent/agent.py](src/content_agent/agent.py)（循环与守卫）、[tools.py](src/content_agent/tools.py)、[llm.py](src/content_agent/llm.py)。

## 目录
```
src/content_agent/  agent.py 循环/守卫/状态 | tools.py 工具与故障注入 | llm.py 模型接入与 MockLLM | cli.py
tests/              test_agent.py
examples/           real/ 真实模型记录 | mock/ 离线记录 | test_results.txt
data/               materials.json  tool_costs.json（合成数据）
docs/               01-05
scripts/            run_examples.sh
```
每个 `examples/*/NN_*.json` 是一次运行的完整输出（含 trace）；`state_*/` 是该用例的状态文件与保存的方案。

## 已知限制（摘要，详见 03）
- 真实模型每个用例只跑了一次，输出非确定；记录不证明稳定性。
- 失败的工具调用不计费，这是本项目的简化约定。
- GitHub 仓库：[wjqCreep/content-task-planning-agent](https://github.com/wjqCreep/content-task-planning-agent)；评审时可在仓库提交记录查看最终 commit SHA。
