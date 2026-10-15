# 会话维护边界：实施前约定

范围：提取 ConversationService 中记忆提炼与短期压缩的任务生命周期，不修改抽取算法、图谱、发送流程、线上提示词或扩展协议。

## 职责与接口

- 唯一实现位于 `agent/conversation/maintenance.py::ConversationMaintenance`。
- 构造注入 `message_count(scope)`、异步 `extract_memory(scope)`、`extract_every`、可选 `ConversationCompactionPort`、提炼并发数（默认 2）和可选 `ScopeWorkBudget`。
- `maybe_extract(scope) -> bool`：实际消息数为正且命中间隔时异步提炼；同 scope 只能有一个待执行或运行任务；不同 scope 共享并发上限。返回是否接受本次请求。
- `after_reply(event) -> bool`：仅成功发送并记账后的调用点使用；跳过 operator 来源、缺失压缩器、同 scope 重复请求和超出 scope 预算的请求。压缩继续用现有每 scope 每分钟 2 次预算，不额外扩大模型消耗。
- `pending_extractions`、`pending_compactions`：只读 scope 集合快照，避免调用者修改内部任务表。
- `aclose()`：先拒绝新维护任务，再取消且等待所有已登记任务（含排队等待槽位的任务）。重复、并发调用可等待相同清理完成；关闭调用者被取消也完成清理再传播取消。
- 错误仅记录，不引入自动重试；任务结束后解除同 scope 的占用。回调必须仅删除自己所属的任务，不能删除后来接受的新任务。
- 维护模块不反向导入 service、orchestrator、features、bootstrap、interfaces 或 infrastructure；只通过回调/现有端口访问模型与存储。日志由构造参数注入，服务集成保留原日志来源。

## 服务集成

`ConversationService` 只持有 `maintenance`，无 extracting、extract_slots、_extract_tasks、_compaction_tasks、_compaction_budget 等重复状态，也不保留旧维护方法的转发壳。入站成功写入后调用 maybe_extract；execute_turn 成功发送记账后调用 after_reply。

关闭时先停止维护准入并启动清理，与观察队列排空独立进行；在 finally 中等待维护清理。因此观察关闭报错或取消不会遗留后台维护请求。

## 验证与顺序

先写独立单元测试（只用内存假端口）与服务集成测试（临时 SQLite），记录真正未实现的 red，再实施；不回退已完成实现来制造 red。测试/适配器/阈值在 red 接受后冻结。覆盖正常间隔、空历史、重复请求、群私聊 scope、跨 scope 并发、失败重入、公平预算、operator 排除、成功回执、关闭取消和集成唯一状态所有者。新模块单独要求行覆盖 >= 90%、分支 >= 80%；保留所有历史检查。正式 gate 只说明配置范围内的验证结果，尚无真实 QQ/付费模型联调。
