# 02 方案

## 调用流程
```
CLI -> Agent.run(topic, budget, deadline)
        |- 输入校验（缺失/非正 -> needs_input，不调工具）
        |- 加载 state_<key>.json（key = hash(主题|预算|时限)）
        |     已 completed 且方案文件在 -> already_completed（不执行、不计费）
        |     否则续跑：success 步骤保留，其余重置为 pending
        '- 循环 (<= max_steps)：
             模型输出 JSON {action,args,reason}
             -> harness 校验：动作合法 / 工具调用上限 / 依赖 / 预算与时限守卫 / 已成功不重复
             -> 执行工具，更新状态与费用，落盘
             -> 把「观察 + 最新状态」作为下一条消息交给模型
             -> 模型选下一动作，直到 finish 或触顶
```
动作集合：`estimate_cost`、`search_materials`、`generate_script(tier)`、`save_plan`、`finish`。依赖：检索 → 脚本 → 保存。

## 谁决定什么
| 由模型决定 | 由 harness 保证 |
|---|---|
| 先估哪个档位、是否降档、降到哪一档 | 步骤依赖、预算/时限不被突破（`blocked_budget`） |
| 检索用什么关键词、检索为空是否换词/停止 | 已成功步骤不重复执行、不重复计费（`already_done`） |
| 可重试错误是否重试、不可重试时降档还是停止 | `finish completed` 必须 `save_plan` 已成功（`blocked_finish`） |
| 何时 finish 及 incomplete 的原因 | 步数与工具调用上限、无法解析的输出作为观察返回 |

模型每步输出的 `reason` 是一句简短的决策依据（引用观察到的数字/错误），记录在 trace 中；不要求也不记录内部思维过程。

## 预算/时限判定
`estimate_cost(tier)` 返回整套方案总价，以及按**当前已花费**算出的剩余所需（已成功的步骤不再计入）。守卫用同一口径：本步及后续未完成步骤合计必须 ≤ 余额。所以「检索后才发现 premium 放不下」会被拦在 generate_script 之前，而不是花完钱才发现。

## 状态与续跑
`state_<key>.json` 保存每步 status（pending/success/failed/insufficient）、attempts、结果、已花费用/耗时、所选档位。失败步骤不会被记为 success。同输入再次运行时，success 步骤的结果和费用保留，只重跑未成功步骤（失败续跑，见用例 06/07）。方案文件名由同一 key 决定，`save_plan` 对相同内容幂等。

## 技术取舍
- **不用框架**：流程只有 3 个业务步骤，守卫与状态是核心，原生实现约 400 行，比为框架适配更容易解释和测试。代价：没有现成的重试/可观测性/并发能力。
- **模型协议用 JSON 文本动作，而非原生 tool-use**：只依赖最基础的 messages 接口，Mock 与真实模型走同一套解析与守卫；代价是需要容错解析，原生 tool-use 的 schema 约束会更稳。
- **模型可以违规，harness 兜底**：守卫放在代码里而不是只靠提示词，`tests/` 里用脚本化 LLM 专门验证「模型坚持超支/越序/谎报完成」时被拦下。
- **降档而不是删步骤**：重规划空间限定为三个档位，便于验证；没有让模型自由改计划结构。
- **MockLLM**：规则写死，读与真实模型相同的观察 JSON，用于离线验证接线。它不能证明真实模型的决策质量。
- **未使用本地沙箱外的任何网络工具**；业务工具全部是本地函数 + 合成数据。

## 故障注入
`--inject-failure tool[:tier]:once|always`。`once` 第一次调用抛 `timeout`（retriable=true），`always` 每次抛 `unavailable`（retriable=false）。可限定到某档位，例如只让 premium 脚本失败。
