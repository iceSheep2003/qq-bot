# 管理界面

`webui/` 包含维护者控制台、配置校验、静态资源与操作手册。
`console/` 保存管理查询服务。

管理 HTTP 请求与 QQ 对话接入分开；QQ 的 OneBot 协议适配在
`infrastructure/adapters/`，在线回合在 `agent/`。
