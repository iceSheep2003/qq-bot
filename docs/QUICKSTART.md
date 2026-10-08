# 安装与启动

这份指南从一个新的源码目录开始，先让测试群里的文字回复跑通，再逐项开启其他能力。已有部署升级时，不要用示例文件覆盖现有 `.env`、任务配置和数据库。

## 开始前

- Python 3.11 或更高版本，以及能创建虚拟环境的运行环境。
- 已安装并登录 QQ 的 NapCat。
- 可用的 OpenAI 兼容聊天接口：API 根地址、密钥和真实可用的模型名。
- 一个允许 Bot 测试和记录消息的群。

基础文字聊天不需要 GPU、Pillow、语音服务或向量服务。图片理解需要模型支持视觉输入；启用工具的功能需要模型支持对应的工具协议。

## 安装项目

在源码根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp .env.example .env
cp config/schedules.example.json config/schedules.json
```

这里只适用于首次安装。`config/schedules.example.json` 是空任务列表，避免首次启动就往群里发送计划消息。真实的 `config/schedules.json` 保留在部署机器，不纳入版本控制。

## 填写配置

打开 `.env`，修改这些字段：

| 字段 | 怎么填 |
| --- | --- |
| `BOT_MODEL_BASE_URL` | 服务商提供的 API 根地址，例如 `https://your-provider.example/v1`；不要填完整的 `/chat/completions` 路径 |
| `BOT_MODEL_API_KEY` | 模型服务密钥 |
| `BOT_MODEL_NAME` | 服务商实际接受的模型 ID，不要照抄别人的模型名 |
| `BOT_ONEBOT_TOKEN` | 一段独立的长随机字符串，NapCat 使用同一个值 |
| `BOT_GROUP_ALLOWLIST` | 先填一个测试群号；多个群号以逗号分隔 |
| `BOT_PRIVATE_ENABLED` | 建议保持 `false`，先不要接收私聊 |
| `BOT_WEBUI_TOKEN` | 另一段长随机字符串，只用于管理界面登录 |

`.env` 会被 shell 加载，因此带空格的值需要加引号，不要把未经检查的外部文本当成配置执行。优先使用 HTTPS 模型接口；HTTP 可能让密钥和聊天内容以明文经过网络。

`.env.example` 不包含可用的模型账号。更换模型服务时，必须重新验证鉴权、模型名和请求格式。此项目不会替你寻找可用中转站。

## 接入 NapCat

### Bot 与 NapCat 同机、都直接运行

保持 `BOT_ONEBOT_HOST=127.0.0.1`、`BOT_ONEBOT_PORT=6199`。在 NapCat WebUI 中新增 WebSocket 客户端：

1. URL 填 `ws://127.0.0.1:6199/ws`。
2. Token 填 `.env` 中的 `BOT_ONEBOT_TOKEN`。
3. 消息格式选择数组。
4. 启动 Bot 后，再启用这条连接。

### 容器或跨机器运行

容器内的 `127.0.0.1` 只指向自身。两个容器在同一个网络时，使用 Bot 的容器服务名，例如 `ws://qunbot:6199/ws`；NapCat 在容器、Bot 在宿主机时，使用容器可访问的宿主机地址。

这种情况下 Bot 通常需要将 `BOT_ONEBOT_HOST` 设置为容器或服务器可接受连接的地址。若监听 `0.0.0.0`，必须同时用容器网络或防火墙限制来源，不要只依赖 Token。

WebUI 仍只监听回环地址。远程管理优先使用 SSH 隧道；不要为方便而将 `6200` 端口直接开放到公网。

## 检查、启动和验证

程序本身不会读取 `.env`。在同一个终端中执行：

```bash
set -a
source .env
set +a
.venv/bin/qunbot --check
.venv/bin/qunbot
```

`--check` 校验本地配置和扩展装配，不发送真实 QQ 消息，也不证明模型端点可用。

先确认监听日志，再确认 NapCat 连接成功。到白名单群里 `@机器人 你好`，按顺序检查：

1. Bot 收到了这条群消息。
2. 模型请求成功返回。
3. QQ 群里收到了回复。

示例配置没有启用 `reply_policy`，所以普通群消息进入上下文，但不会触发普通文字回复。其他入站互动仍由各自的模块判断，不要把“未 @ 不回文字”理解成完全不处理消息。

WebUI 默认地址为 `http://127.0.0.1:6200/`。登录后可以查看配置和独立 Guide；保存配置后，重新加载 `.env` 并重启 Bot。长期运行可交给服务管理器，但不要同时启动两个实例来读取同一份数据库、连接同一个 QQ 账号。

## 逐项开启功能

`BOT_EXTENSIONS` 选择要装配的包；包自己的 `BOT_*_ENABLED` 等参数决定具体行为。只开开关、不注册对应扩展，或只注册扩展、不满足其配置要求，都不等于已经启用。

### 主动接话

在已有的 `BOT_EXTENSIONS` 中加入 `reply_policy`，设置：

```dotenv
BOT_REPLY_POLICY_ENABLED=true
BOT_REPLY_POLICY_MODE=room
```

先用操作手册确认每日额度、冷却、接话概率和追问窗口，别一开始就把概率拉满。被 @ 也会经过策略判断，不是无条件调用模型。

### 语音

在 `BOT_EXTENSIONS` 中加入 `voice`，再填写 `.env.example` 的 `BOT_TTS_PROVIDER`、`BOT_TTS_BASE_URL`、`BOT_TTS_API_KEY`、`BOT_TTS_MODEL` 和 `BOT_TTS_VOICE`。

模型与音色名称必须是当前账号可用、且该适配器支持的组合。默认示例是 CosyVoice 配置，不表示所有账号都能调用它。planner 使用统一的语音概率门控，目前默认 `0.20`，并限制到 15 字以内的短语音；实际占比还会因内容、长度和服务失败而降低。

先试听，不发到群里：

```bash
.venv/bin/python scripts/voice_smoke.py --output /tmp/qunbot-voice-smoke.wav
```

脚本的群发送参数是显式的。需要实机测试时先看 `--help`，只选白名单测试群，发送结果不确定时先查看群历史，不要连续重试。

### 表情包

项目自带 Kinna 素材，映射在 `memes/catalog.json`。启用 `memes` 后，只有可读取且通过校验的素材才会进入可用目录。第三方图包需要单独取得，来源与恢复步骤见 [第三方说明](../THIRD_PARTY_NOTICES.md)。

`BOT_MEDIA_FOLLOWUP_MEME_PROBABILITY` 控制文字后的表情补发概率，示例为 `0.40`；它是符合条件时的概率，不是所有消息必须达到的发送占比。

### 定时任务和海报

在 `BOT_EXTENSIONS` 中加入 `scheduled_chat`，编辑本地 `config/schedules.json`，或使用 WebUI 的任务管理。首次添加建议先设较远的时间，核对群号和时区，再启用。

下面是一个每天中午的 `chat` 任务示例，**复制后会产生真实发言**：

```json
{
  "jobs": [
    {
      "id": "daily-noon",
      "group_id": "123456789",
      "kind": "cron",
      "value": "30 12 * * *",
      "action": "chat",
      "prompt": "午休时间，结合眼前聊天说一句简短自然的话。"
    }
  ]
}
```

将群号改成自己的白名单群。`prompt` 是给模型的场景说明，不是固定发送文本。`at`、`every`、`cron` 的排期和执行记录详见操作手册；任务本身仍受它对应 handler 的发送规则约束。

考研倒计时海报是独立的 `exam_poster` 扩展，还需要安装可选依赖、填写考试日期和可用中文字体：

```bash
.venv/bin/python -m pip install -e '.[exam-poster]'
```

高校照片、校徽轮换仍未完整接入，当前不要依赖该目录生成校园轮换海报。显式定时任务与静默续聊不是同一策略；`continuation` 的建议任务默认停用，不会仅因启用包就自动开始发送。

### 群管理

加入 `qq_admin` 前，先检查 QQ 账号的实际管理权限，并逐项看清禁言时长、入群审核、欢迎词和头衔投票参数。建议在测试群里验证，不要直接对生产群开放整套动作。

## 排错与升级

| 现象 | 先检查 |
| --- | --- |
| NapCat 连不上 | Bot 进程是否启动、端口、`/ws`、Token 和容器网络；401 多为认证问题 |
| 收到消息却没有回复 | 群白名单、是否 @、回复策略的概率/额度/冷却，再查模型错误 |
| 模型请求失败 | HTTP 状态码和错误类型：鉴权、模型不存在、限流、超时或服务停运；不要只靠换一个模型名排错 |
| QQ 文字成功、图片或语音失败 | 素材路径、媒体格式、NapCat 所在机器能否读取文件和接口返回值 |
| 重启后配置没有改变 | 新进程是否重新加载 `.env`，服务管理器是否另有环境配置 |
| QQ 被强制下线 | 检查 NapCat 的登录状态；watchdog 不能绕过扫码或设备验证 |

升级前备份本地数据库、`.env`、任务和人格文件。不要提交运行日志、聊天数据库、试听数据或密钥；分享问题时只提供去标识化的复现步骤和必要日志。
