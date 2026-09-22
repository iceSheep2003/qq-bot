# QunBot (MVP)

后续开发者请先读 [模块现状与接手开发路线](docs/DEVELOPMENT_ROADMAP.md)：逐包列出已实现能力、缺口、实施顺序、并行边界与验收标准。

一个可扩展的 Python QQ 群伴侣 Bot 原型。使用 NapCat 的 OneBot v11 反向 WebSocket 接入；群聊中默认仅被 @ 时回复，其他群消息仅用于当前群上下文与后台记忆提炼。私聊、随机主动发言默认关闭。**所有配置只能通过本地代码/配置文件修改，聊天消息没有管理命令。**

## 启动

1. Python 3.11+，在项目目录执行 `python3 -m venv .venv`，然后执行 `.venv/bin/python -m pip install -e .`。启用考研海报时改用 `pip install -e '.[exam-poster]'`。
2. 参考 `.env.example` 设置环境变量。程序**不会自动读取** `.env`；可由 shell、systemd 或容器传入。不要把真实密钥写入 `.env.example` 或提交到仓库。
3. NapCat WebUI 中打开「网络配置 → 新建 → WebSocket 客户端」，URL 填 `ws://127.0.0.1:6199/ws`，启用该连接，Token 与 `BOT_ONEBOT_TOKEN` 相同，消息格式选数组。若跨机器部署，修改监听地址并通过防火墙限制来源；Docker 中 `127.0.0.1` 指当前容器，需改用 Bot 容器名。
4. 运行 `.venv/bin/qunbot --check` 检查配置，再运行 `.venv/bin/qunbot`。

`BOT_GROUP_ALLOWLIST` 是允许接入的群号，多个群用逗号分隔。`BOT_PRIVATE_ENABLED=false` 时私聊会被直接忽略、不会写入记忆；如需开放私聊，先考虑增加发送者白名单。不要把 API Key 或 OneBot Token 提交到仓库。

你提供的 Grok 代理地址可填 `BOT_MODEL_BASE_URL=http://216.167.7.16:8080/v1`，模型填 `BOT_MODEL_NAME=grok`，密钥填 `BOT_MODEL_API_KEY`。代码使用 OpenAI 兼容的 `/chat/completions`；该代理是否支持此格式、工具调用和图片输入仍需实际验证。**该地址使用 HTTP，Bearer 密钥及对话可能明文经过网络；建议让服务提供 HTTPS 后再传真实密钥。**

语音由 `BOT_EXTENSIONS` 中是否包含 `voice` 控制；未启用时不会创建客户端。启用时配置 `BOT_TTS_PROVIDER=dashscope`、`BOT_TTS_BASE_URL=https://dashscope.aliyuncs.com/api/v1`、`BOT_TTS_MODEL=qwen3-tts-flash`、`BOT_TTS_VOICE=Cherry` 和 `BOT_TTS_API_KEY`。此适配器调用百炼非流式 Qwen-TTS，再发送 OneBot `base64://` 音频；实际语音格式仍需 NapCat 联调。

建议先启动 Bot 并确认输出 `reverse WebSocket listening`，再启用 NapCat 的 WebSocket 客户端；NapCat 显示连接成功后，在白名单群里 `@机器人 你好` 测试。群内不 @ 的消息默认只记上下文，不回复；私聊仅在 `BOT_PRIVATE_ENABLED=true` 时开放。若连接失败，先核对端口、`/ws` 路径、Token、容器网络和 Bot 日志中的 401/404；不要在聊天里发送密钥排错。

## 当前能力

- OneBot v11 群聊与私聊文本收发、@ 识别、图片 URL 透传给支持视觉的模型、按消息 ID 去重。
- 本地表情包素材目录与标签选择；可选 OpenAI 兼容 TTS 将短回复发为 QQ 语音。表情和语音通过独立媒体端口处理，不进入 Agent 工具循环。
- SQLite 会话记录、每位群友独立好感度、变化原因及每小时一次的限幅更新。回复后的独立评估器只在有明确关系意义时建议小幅变化；数据库操作不向聊天用户开放。可通过 `BOT_AFFECTION_AUTO_ENABLED=false` 关闭自动评估。
- SQLite FTS5 长期记忆、模型只读搜索工具、每 N 条消息后台提炼事实。当前尚无 LivingMemory 的向量、图谱、TTL 和管理面板。
- 机器人**自己的心情**：`qunbot/emotion/` 维护心情/精力/压力/兴致/社交意愿五个维度，会随互动起落、并按半衰期自然回落到中性基准。回复后的独立评估器给出小幅变化；注入提示词时只给自然语言心境（分档措辞 + 一句最近的心事），不下发任何数值。心情差到低于 `BOT_MOOD_PROACTIVE_MIN_SOCIABILITY` 时不会主动水群，但**定时任务不受影响**，也绝不会因此失礼或迁怒。可通过 `BOT_MOOD_ENABLED=false` 整体关闭，或 `BOT_MOOD_AUTO_ENABLED=false` 只关掉自动评估。
- `skills/*/SKILL.md` 指令型 Skill 按触发词加载；修改后重启生效。Skill 不能执行任意代码。
- 随机主动群聊是可选的 `proactive_chat` 扩展，默认未启用。开启后仍受白天时段、群白名单、近期活跃度、冷却、每日额度、随机门控与重复内容过滤限制。
- 在 `config/schedules.json` 定义 `at`、`every` 或五段 `cron` 任务，重启后加载并持久化运行记录。聊天参与者不能创建、关闭或修改任务。
- 定时任务有两种动作：`chat`（结合群上下文水一句）与 `poster`（发考研倒计时海报）。海报由 Pillow 本地绘制，模型只负责配文，因此模型服务不可用时海报照发不误。
- 日志记录每轮稳定前缀 SHA-256 摘要和模型用量；是否返回 `cached_tokens` 取决于模型服务商。

## 扩展边界

本项目采用端口与适配器结构。`ports.py` 声明会话、人物、记忆、任务、模型和消息发送端口；`runtime/agent.py` 与 `runtime/service.py` 不依赖具体扩展。记忆提炼／召回属于核心 `qunbot/memory/`；定时基础设施在 `qunbot/scheduling/`，只负责排期、注册表和通用守卫。`qunbot/extensions/loader.py` 依照本地 `BOT_EXTENSIONS` 显式装配任务动作、动态上下文、模型可调用工具、媒体、回复后观察器和后台 worker。禁用海报时不导入 Pillow；禁用语音时不创建语音客户端。Skill 只说明如何使用已启用能力，禁用功能对应的 Skill 不进入 Agent 的目录。人格在 `config/persona.md`。

代码所有权分为：核心对话链路（QQ 接入、Agent、记忆、每位群友的关系／好感度），基础设施（定时引擎、SQLite 连接）与可选功能包（定时闲聊、海报、随机续聊、情绪、表情、语音）。`BOT_EXTENSIONS` 仅接受 `loader.py` 本地允许名单，群消息不能安装代码。会话、关系、记忆、活动和任务各有独立 SQLite repository 和迁移函数；它们共享一个连接／事务，不共享业务写入接口。跨模块遗忘操作独立放在 `storage/privacy.py`。

情绪自成一个有界上下文：`qunbot/emotion/` 自带领域模型、SQLite、评估器和配置；`extensions/mood/register.py` 将其注册成动态上下文、回复后观察器以及主动续聊的情绪门控。核心只认识通用观察器／上下文端口，不持有情绪字段。

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

### 新增一种定时任务类型

定时任务引擎是独立包，任务类型由本地配置显式注册，调度路径上没有 `if action == ...`：

```
qunbot/scheduling/
  registry.py     JobHandler / JobRuntime / 注册表 / suggested_jobs
  guards.py       可复用的发送前检查（配额、冷却、群冷清、去重）
  scheduler.py    at/every/cron 引擎、配置校验、job_runs 落库
qunbot/extensions/
  loader.py       ← 根据 BOT_EXTENSIONS 显式装配
  scheduled_chat/job.py
  exam_poster/job.py
  exam_poster/renderer.py
```

新增一种任务需实现 handler，再在本地 loader 中登记并启用：

```python
# qunbot/extensions/rollcall/job.py
from qunbot.scheduling.registry import JobRuntime, suggestion

class RollCallJobHandler:
    action = "rollcall"

    def suggested_jobs(self):
        return [suggestion("daily-rollcall", "0 22 * * *", "提醒大家今天打卡")]

    async def run(self, bot: JobRuntime, job: dict) -> None:
        # bot 是 JobRuntime，仅提供通用的任务运行能力：
        # policy / agent / conversations / activity / sender /
        # last_reply，以及 scope_lock()、send_reply()、today_start()、
        # local_today()、job_event()。
        clean = await bot.send_reply(job["group_id"], None, "今天也别忘了打卡～")
        if clean:
            guards.record(bot, job, bot.job_event(job, 0), clean, "rollcall")
```

在 `extensions/loader.py` 的允许名单添加模块工厂，由该模块的 `register_jobs(registry, config)` 注册 `RollCallJobHandler`，再把 `rollcall` 加入本地 `BOT_EXTENSIONS`。这一步由部署者改代码／配置完成，群成员没有启用任务代码的入口。

**handler 自带的任务默认是停用的。** `suggested_jobs()` 声明的排期会展开到每个白名单群、以 `enabled=0` 写进库（key 形如 `daily-rollcall@866795853`），所以丢一个文件绝不会自己开始往群里发东西。启用方式是把 `--check` 输出里的 `to_enable` 片段粘进 `config/schedules.json`，写进去即接管该 key 并置为启用。已经配了相同「群 + 动作 + 排期」的任务时，建议不会再重复出现。

约定：

- `run` 里对「这次不该发」的情况抛 `JobSkipped`，调度器记为 `skipped`；抛其他异常记为 `failed`。
- 群白名单和活跃时段由 `scheduling/runner.py` 的 `JobRunner.run` 统一把关，handler 绕不过去。
- 配额、冷却、群冷清、去重这些只对「像群友说话」有意义的检查在 `guards.py`，按需调用；海报任务一个都不用。

这还是原型：文本群聊链路已在测试群跑通；海报、语音的实际发送兼容性仍需联调。黑话学习、向量记忆、完整关系管理 API 尚未实现，不应视为已交付。
