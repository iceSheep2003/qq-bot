# 动作与执行

`tools/registry.py` 管理既有工具与权限，`tools/kernel.py` 实现固定工具契约和执行校验。
`replies/` 解析候选、规划文字和媒体并处理发送回执。
`interactions/` 管理 QQ 原生互动策略。
`scheduling/` 校验声明式任务并安排已有动作。

真实 QQ 或数据库操作由注入的基础设施实现完成，用户 Skill 不能注册任意工具。
