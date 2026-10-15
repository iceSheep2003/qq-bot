# 回复后观察边界（实施前约定）

本轮范围：从 ConversationService 提取回复后观察；不改模型、工具、图谱、Skill 或排期协议。

- `agent/observation.py::ObservationDispatcher` 是观察队列、去重缓存、消费者注册、统计和关闭状态的唯一所有者。
- 对外接口：`dispatch(event, reply, draft=None)`、`submit(key, observer, event, reply)`、`pending`、`stats`、`run_durable()`、`aclose(timeout=5)`。
- 构造注入观察者、群好感度开关、容量/并发/去重上限、可选 ObservationRepository；不导入具体存储、bootstrap、features、interfaces 或 service。
- `contracts/ports.py::ObservationRepository` 描述现有 outbox 的 enqueue/recover/lease/complete/failed/pending 协议；SQLite 实现和恢复策略不变。
- ConversationService 仅委托 dispatch/submit/durable loop/pending/stats 和关闭，不复制队列状态；记忆提炼、压缩暂留原处。
- 普通观察适用于群聊与私聊；自动好感度仍仅允许群聊。带状态的 ReplyDraft 走 observe_draft。
- 内存模式保持有界 FIFO、有限窗口去重、失败不重试、满载丢弃并计数。持久模式保持消费身份和明确允许重放的白名单，由仓储决定重试。
- 关闭后拒绝新工作；内存队列限时排空；取消并等待消费者，包括运行中的持久消费者。关闭被取消仍须清理消费者。
- 日志可注入，服务集成保留现有日志来源，避免破坏监控与原有断言。

先新增独立验收测试并记录 cqg red，再实现。原测试及阈值保留；新模块独立统计 90% 行/80% 分支覆盖，避免汇总掩盖退化。
