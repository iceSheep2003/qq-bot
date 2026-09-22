# QunBot (MVP)

一个可扩展的 Python QQ 群伴侣 Bot 原型。使用 NapCat 的 OneBot v11 反向 WebSocket 接入；群聊中默认仅被 @ 时回复，其他群消息仅用于当前群上下文与后台记忆提炼。私聊自动回复。主动发言默认关闭。**所有配置只能通过本地代码/配置文件修改，聊天消息没有管理命令。**

## 启动

1. Python 3.11+，在项目目录执行 `python3 -m venv .venv`，然后执行 `.venv/bin/python -m pip install -e .`。
2. 参考 `.env.example` 设置环境变量。程序**不会自动读取** `.env`；可由 shell、systemd 或容器传入。不要把真实密钥写入 `.env.example` 或提交到仓库。
3. NapCat WebUI 中打开「网络配置 → 新建 → WebSocket 客户端」，URL 填 `ws://127.0.0.1:6199/ws`，启用该连接，Token 与 `BOT_ONEBOT_TOKEN` 相同，消息格式选数组。若跨机器部署，修改监听地址并通过防火墙限制来源；Docker 中 `127.0.0.1` 指当前容器，需改用 Bot 容器名。
4. 运行 `.venv/bin/qunbot --check` 检查配置，再运行 `.venv/bin/qunbot`。

`BOT_GROUP_ALLOWLIST` 是允许接入的群号，多个群用逗号分隔。`BOT_PRIVATE_ENABLED=false` 时私聊会被直接忽略、不会写入记忆；如需开放私聊，先考虑增加发送者白名单。不要把 API Key 或 OneBot Token 提交到仓库。

你提供的 Grok 代理地址可填 `BOT_MODEL_BASE_URL=http://216.167.7.16:8080/v1`，模型填 `BOT_MODEL_NAME=grok`，密钥填 `BOT_MODEL_API_KEY`。代码使用 OpenAI 兼容的 `/chat/completions`；该代理是否支持此格式、工具调用和图片输入仍需实际验证。**该地址使用 HTTP，Bearer 密钥及对话可能明文经过网络；建议让服务提供 HTTPS 后再传真实密钥。**

语音由 `BOT_TTS_ENABLED` 独立控制，默认 `false`。关闭时可保留其余语音配置，但不会创建语音客户端或发起语音请求。阿里百炼语音选 `BOT_TTS_PROVIDER=dashscope`，北京地域示例为 `BOT_TTS_BASE_URL=https://dashscope.aliyuncs.com/api/v1`、`BOT_TTS_MODEL=qwen3-tts-flash`、`BOT_TTS_VOICE=Cherry`；密钥只放 `BOT_TTS_API_KEY`。百炼不同地域的 Key 和入口可能不同，按实际开通地域调整。此适配器调用百炼的非流式 Qwen-TTS 接口，再把返回音频转成 OneBot 可发送的 `base64://`。需要模型回复中含 `[[voice]]` 才会触发发语音；实际 QQ 语音格式兼容性仍需 NapCat 联调。

建议先启动 Bot 并确认输出 `reverse WebSocket listening`，再启用 NapCat 的 WebSocket 客户端；NapCat 显示连接成功后，在白名单群里 `@机器人 你好` 测试。群内不 @ 的消息默认只记上下文，不回复。私聊会直接回复。若连接失败，先核对端口、`/ws` 路径、Token、容器网络和 Bot 日志中的 401/404；不要在聊天里发送密钥排错。

## 当前能力

- OneBot v11 群聊与私聊文本收发、@ 识别、图片 URL 透传给支持视觉的模型、按消息 ID 去重。
- 本地表情包素材目录与标签选择；可选 OpenAI 兼容 TTS 将短回复发为 QQ 语音。表情和语音通过独立媒体端口处理，不进入 Agent 工具循环。
- SQLite 会话记录、每位群友独立好感度、变化原因及每小时一次的限幅更新。回复后的独立评估器只在有明确关系意义时建议小幅变化；数据库操作不向聊天用户开放。可通过 `BOT_AFFECTION_AUTO_ENABLED=false` 关闭自动评估。
- SQLite FTS5 长期记忆、模型只读搜索工具、每 N 条消息后台提炼事实。当前尚无 LivingMemory 的向量、图谱、TTL 和管理面板。
- `skills/*/SKILL.md` 指令型 Skill 按触发词加载；修改后重启生效。Skill 不能执行任意代码。
- 主动群聊默认关闭。开启后仍受白天时段、群白名单、近期活跃度、冷却、每日额度、随机门控与重复内容过滤限制。
- 在 `config/schedules.json` 定义 `at`、`every` 或五段 `cron` 任务，重启后加载并持久化运行记录。聊天参与者不能创建、关闭或修改任务。
- 定时任务有两种动作：`chat`（结合群上下文水一句）与 `poster`（发考研倒计时海报）。海报由 Pillow 本地绘制，模型只负责配文，因此模型服务不可用时海报照发不误。
- 日志记录每轮稳定前缀 SHA-256 摘要和模型用量；是否返回 `cached_tokens` 取决于模型服务商。

## 扩展边界

本项目采用端口与适配器结构。`ports.py` 声明会话、人物、记忆、任务、模型和消息发送端口；`agent.py` 与 `service.py` 仅依赖端口；`app.py` 是唯一装配入口。`OneBotGateway`、`ModelClient` 和 `Store` 分别实现平台、模型与存储适配器。新增表情包、TTS、向量检索或黑话审核时，先定义端口与策略，再在装配入口挂载实现；不往 Agent 循环里继续堆 `if name == ...`。Skill 只定义何时/怎样使用能力，不能代替受控代码实现。人格在 `config/persona.md`，无需改 Python 源码。

定时任务只通过本地 `config/schedules.json` 定义，例如：

```json
{
  "jobs": [
    {
      "id": "kaoyan-daily-poster",
      "group_id": "123456789",
      "kind": "cron",
      "value": "0 7 * * *",
      "action": "poster",
      "prompt": "用一句话给正在备考的同学打气，20 字以内"
    },
    {
      "id": "water-noon",
      "group_id": "123456789",
      "kind": "cron",
      "value": "30 12 * * *",
      "prompt": "午休时间，结合最近群里的聊天随口接一句"
    }
  ]
}
```

`prompt` 是**给模型看的场景指令**，不是要发出去的原话：它会被拼成一条伪消息投进对话，模型看到完整群上下文后自己决定说什么。`action` 省略时默认为 `chat`。

每项必须有稳定唯一的 `id`。修改配置后重启生效；同一个 `id` 的未变更任务保留下一次运行时间。错过的执行不会在重启后补发。

各类计划发言的配额互相独立，不会互相挤占：`chat` 任务用 `BOT_JOB_DAILY_LIMIT`，随机主动发言用 `BOT_PROACTIVE_DAILY_LIMIT`，`poster` 不受日额度限制。所有任务都受 `BOT_ACTIVE_START_HOUR`–`BOT_ACTIVE_END_HOUR`（默认 7–23 点）限制，落在窗口外会被记为 `skipped` 而不是 `failed`。

`chat` 任务还有两道额外闸门：距机器人上次在该群发言不足 `BOT_JOB_COOLDOWN_MINUTES`（默认 30 分钟）不发；群里超过 `BOT_JOB_FRESHNESS_MINUTES`（默认 180 分钟）没人说话不发。生成结果超过 150 字或与最近发言重复也会被丢弃。`poster` 任务不受这两道闸门限制——它是固定要送达的。

`poster` 需要 `BOT_EXAM_DATE`（如 `2026-12-19`）。字体默认按系统常见中文字体依次探测，可用 `BOT_POSTER_FONT` 指定；`qunbot --check` 会提前校验字体能否加载。考试日期过后海报任务自动跳过。

这还是原型：未接真实 QQ 环境验证；图片 URL 的有效期、语音消息格式和模型兼容性需按所用 NapCat/模型服务商实测。黑话学习、向量记忆、完整关系管理 API 尚未实现，不应视为已交付。
