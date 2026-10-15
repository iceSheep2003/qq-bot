# 具体技术接入

`adapters/` 接入 OneBot、模型接口、多媒体与结构化输出。
`storage/` 实现 SQLite 及各类仓储。
`extraction/` 保存依赖具体模型适配器的行为抽取。
`tool_handlers.py` 把固定工具动作连接到具体仓储和网关。

这里实现公共端口，由 `bootstrap/app.py` 选择和注入。
