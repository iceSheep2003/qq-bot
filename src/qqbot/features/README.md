# 可选内置功能

`manifest.py` 是开发者维护的固定清单，`loader.py` 校验并装配启用的功能。
`features.py` 定义宿主贡献接口。其他目录保存人格、黑话、语音、群管理等内置实现。

这些目录由版本代码维护。`BOT_EXTENSIONS` 只能选择已有清单项；
外部新增目录不会自动执行。部署者扩展入口是仓库顶层 `skills/` 和声明式任务。
管理界面已经归到 `interfaces/webui/`；媒体处理归到 `actions/replies/`。
