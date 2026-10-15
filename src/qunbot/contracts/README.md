# 公共契约

`models.py` 定义公共消息、配置策略与结果类型；`ports.py` 定义应用端口。
`cognition.py` 定义认知决策返回值。
`kernel_v1.py`、`lifecycle.py` 和 `extension_inputs.py` 定义固定协议与声明式输入。
`schema.py` 提供校验；`ontology/` 保存当前本体版本；`content.py` 提供共享内容规则。

这里不能导入 Agent、动作、管理界面、功能装配或具体基础设施。
