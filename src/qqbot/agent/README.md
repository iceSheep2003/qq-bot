# Agent 回合与能力

`service.py` 负责准入、回复触发、回合执行和发送结果记账；`observation.py` 负责回复后观察的有界队列、持久恢复和关闭清理。
`orchestrator.py` 负责稳定前缀、动态上下文、模型与工具循环。
`conversation/maintenance.py` 负责长期记忆提炼与短期压缩的后台调度、scope 占用、并发和退出清理；实际提炼和压缩仍通过注入端口执行。

子包：`perception/` 输入理解，`conversation/` 场景与工作记忆，
`identity/` 自身规则与情绪，`memory/` 长期事实、证据和关系，
`cognition/` 可选认知通道，`skills/` 声明式能力说明。

`context.py` 管理上下文贡献；`fairness.py` 管理范围预算。
主循环通过 `contracts/ports.py` 接入实际模型和存储。
