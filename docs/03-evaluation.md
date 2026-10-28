# 03 评估

运行记录在 `examples/real/`（真实模型 `claude-sonnet-5-5`，经 Anthropic 兼容接口）和 `examples/mock/`（离线 MockLLM，只验证流程）。单元测试结果见 [examples/test_results.txt](../examples/test_results.txt)（20 项全部通过，使用脚本化 LLM 与 MockLLM，不访问网络）。
下表「实际结果」均取自 `examples/real/` 中对应文件，不是预期值。

## 验收点对照
| 验收点 | 输入 | 预期 | 实际结果（真实模型） | 记录 |
|---|---|---|---|---|
| 正常完成 + 预算超限重规划 | 咖啡, 预算 10, 12h | premium 估 12 超限 → 改 standard(8) → 检索、脚本、保存 | 先估 premium(12, affordable=false)，再估 standard(8)，三步成功，费用 8.0，completed | [real/01](../examples/real/01_normal_over_budget_replan.json) |
| 重复执行 | 同上再运行一次 | 不执行、不计费、不重复写文件 | `already_completed`，trace 为空，本次费用 0 | [real/02](../examples/real/02_repeat_run.json) |
| 缺预算 | 咖啡, 无预算 | 不调工具、无费用 | `needs_input`，缺少 budget，trace 为空 | [real/03](../examples/real/03_missing_budget.json) |
| 工具失败 + 保留成功步骤 | 咖啡, 预算 20, 注入 premium 脚本持续故障 | 检索不重跑；脚本降档 | 检索成功(attempts=1)；premium 脚本 `unavailable`(retriable=false)；模型估算 standard 后生成脚本(generate_script attempts=2)，保存，费用 8.0，completed | [real/04](../examples/real/04_tool_failure_replan.json) |
| 无可行方案 | 咖啡, 预算 5 | incomplete + 缺口，不保存 | 依次估 premium/standard/lite(12/8/6)，`incomplete`，费用 0，原因含各档缺口(lite 缺 1.0) | [real/05](../examples/real/05_no_feasible_plan.json) |
| 失败步骤不记为成功 | 春节, 预算 20, 保存持续故障 | save_plan=failed，不得 completed | 检索与 premium 脚本成功，save_plan `failed`，模型 finish incomplete，费用 10.0，无方案文件 | [real/06](../examples/real/06_save_fails_then_resume_1.json) |
| 失败续跑（加分） | 同上，故障解除后再跑 | 只执行保存 | 估算后直接 save_plan，completed，本次费用 2.0，累计 12.0；检索/脚本 attempts 仍为 1 | [real/07](../examples/real/07_save_fails_then_resume_2.json) |
| 可重试错误 | 开学, 预算 20, 保存首次超时 | 重试一次 | save_plan 第 1 次 `timeout`(retriable=true)，第 2 次成功，completed，费用 12.0 | [real/08](../examples/real/08_transient_error_retry.json) |
| 步数上限 | 咖啡, 预算 10, `--max-steps 3` | 明确未完成状态 | `incomplete`，原因 `step_limit_reached`，已完成的检索保留(费用 2.0)，其余 pending | [real/09](../examples/real/09_step_limit.json) |
| 资料不足 | 主题「不存在的主题」 | 不编造素材，incomplete | 两次检索均 count=0，`incomplete`，说明缺少相关素材，费用 4.0 | [real/10](../examples/real/10_no_materials.json) |

「模型根据不同工具结果调整动作」的两条路径：01（观察到 affordable=false → 降档）与 04（观察到工具 unavailable → 估算更低档后改用 standard，保留检索结果）。另有 06/07（保存失败 → 停止，再次运行只续跑保存）、08（timeout → 重试）。每条 trace 含步骤、工具参数、返回摘要、决策依据(`reason`)和执行后状态。

## 守卫与上限的单元测试
`tests/test_agent.py` 用脚本化 LLM 模拟「模型犯错」：越序调用 save_plan（`blocked_dependency`）、坚持 premium 导致超支（`blocked_budget`，费用不变）、未保存就 finish completed（`blocked_finish`）、输出非 JSON（作为观察返回，不崩溃）、重复执行已成功步骤（`already_done`，不再扣费）。工具调用上限 `--max-tool-calls` 只在单元测试（`test_tool_call_limit`，使用 MockLLM）中验证，`examples/` 里**没有**对应的运行记录，真实模型下也未验证。

## 失败与发现
1. **首轮真实运行的检索行为不合格，已改提示词并重跑。** 最初给模型的状态里带有 `known_tags`（全部素材标签），模型对「不存在的主题」用无关的通用关键词（活动/优惠/新品）检索到 4 条素材并 completed，只在结论里提醒需人工复核。这是把资料不足当成成功。处理：去掉 `known_tags`，在系统提示词加规则 6（检索为空时不得拿无关关键词凑数）。重跑后（real/10）模型换了一次与主题相关的关键词，仍为空即停止。`examples/real/` 只保留重跑后的结果。
2. **首次调用真实模型失败**：接口拒绝 `temperature=0`（该模型不支持非默认温度），已从请求中去掉。
3. **多余的估算调用**：real/08 第 4 步在没有新信息的情况下重复估算了一次 premium，浪费一次工具调用（估算免费，但占用步数）。
4. **资料不足也计费**：real/10 两次空检索各扣 2.0，共 4.0。这是「已调用即计费」的结果，空检索是否收费是业务约定，此处未做区分。

## 限制
- 真实模型每个用例只跑一次，输出非确定，记录不能证明稳定性或决策质量；没有做多次重复统计。
- `examples/real/` 和 `examples/mock/` 在最后一次改动 `agent.py`（补充「状态已完成但方案文件丢失时只重做保存」，对应测试 `test_completed_state_but_plan_file_missing_resaves_only`）之前生成。这次改动不影响这些用例走过的路径，但这些记录不是由最终代码重新生成的。
- 失败的工具调用不计费；失败时的计费规则是简化约定。
- 重规划空间仅限三个脚本档位，不支持模型自行增删步骤。
- 素材检索是关键词子串匹配，不含语义检索。
- 状态以 `主题|预算|时限` 为 key；不同写法的主题（如「咖啡 」与「咖啡」）视为不同任务。
- Mock 只验证接线，不代表模型能力；`examples/mock/` 里的 trace 是写死规则产生的。
- 状态文件为本地 JSON，无并发保护。
