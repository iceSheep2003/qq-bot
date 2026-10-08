# Kinna · QQ Bot

基于 Python 和 NapCat 的 QQ 群聊机器人，支持长期记忆、角色人格、表情包和语音互动。

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![QQ](https://img.shields.io/badge/QQ-NapCat-2563EB)
![OneBot](https://img.shields.io/badge/OneBot-v11-2563EB)

[快速开始](#快速开始) · [使用文档](#使用文档) · [开发指南](CONTRIBUTING.md) · [反馈问题](https://github.com/iceSheep2003/qq-bot/issues)

## 项目简介

Kinna 侧重 QQ 群里的日常聊天：参与多人话题、接梗、发表情，以及根据与群友的关系调整互动方式。项目自带 Kinna 人设，也支持修改角色和说话风格。

NapCat 负责 QQ 登录和消息收发，Bot 负责对话、记忆和行为规划，语言模型通过 OpenAI 兼容接口接入。配置和聊天记录保存在部署机器上，管理界面随 Bot 启动。

当前为持续开发中的版本。聊天效果依赖模型与配置，功能的实现状态和限制见 [功能说明](docs/FEATURES.md)。

## 功能概览

| 功能 | 支持内容 |
| --- | --- |
| 群聊对话 | @ 回复、主动接话、引用回复、连续对话窗口和较早对话的压缩 |
| 记忆与关系 | 长期事实与来源记录、中文检索、可选向量召回、按群友独立记录好感度 |
| 人格与学习 | 可编辑人设、动态心情、群内黑话与表达风格学习、对话示例更新 |
| 回复方式 | 文字、表情包、QQ 表情、短语音；长文字自动拆条，避免文字和语音重复 |
| QQ 互动 | 消息贴表情、戳一戳、跟风戳，以及带持久化去重的 `+1` 复读 |
| 主动与定时发言 | 安静后的续聊、一次性任务、间隔任务、cron 排期和倒计时海报 |
| 群管理 | 禁言、精华消息、群文件引导、入群审核、欢迎词和需征得同意的头衔提议 |
| WebUI | 配置编辑、记忆浏览、关系图谱、定时任务管理和独立操作手册 |

### 默认行为

使用仓库中的 `.env.example` 时：

- 只处理白名单群，私聊关闭；普通文字聊天默认仅在被 @ 时回复。
- 装配表情包、情绪、人格、黑话、风格学习和 WebUI，长期记忆与关系系统也会运行。
- 主动接话、静默续聊、语音、定时任务和群管理需要另外启用与配置。

先在一个测试群里跑通文字回复，再逐项开启其他能力。

## 快速开始

### 运行要求

- Python 3.11+。
- 已安装并登录 QQ 的 [NapCat](https://github.com/NapNeko/NapCatQQ)。
- 一个可用的 OpenAI 兼容聊天接口，以及它的地址、密钥和模型名。

基础聊天不需要 GPU 或语音服务。以下命令使用 Linux/macOS 的 Bash 或 Zsh；容器网络、可选功能和排错步骤见 [完整启动指南](docs/QUICKSTART.md)。

### 1. 下载并安装

首次安装执行：

```bash
git clone https://github.com/iceSheep2003/qq-bot.git
cd qq-bot
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp .env.example .env
cp config/schedules.example.json config/schedules.json
```

已有部署升级时，不要用示例文件覆盖自己的配置或数据库。

### 2. 填写配置

编辑 `.env`，替换下面的占位值：

```dotenv
BOT_MODEL_BASE_URL=https://your-provider.example/v1
BOT_MODEL_API_KEY=your-model-api-key
BOT_MODEL_NAME=your-chat-model
BOT_ONEBOT_TOKEN=your-long-random-onebot-token
BOT_GROUP_ALLOWLIST=123456789
BOT_WEBUI_TOKEN=your-separate-long-random-webui-token
```

模型名以服务商实际提供的 ID 为准，群号改成自己的测试群。OneBot 和 WebUI 使用两份独立的 Token。仓库不附带模型账号或可用中转地址。

### 3. 配置 NapCat

在 NapCat WebUI 的网络配置中新增 **WebSocket 客户端**。Bot 与 NapCat 直接运行在同一台机器时填写：

| 字段 | 配置 |
| --- | --- |
| URL | `ws://127.0.0.1:6199/ws` |
| Token | 与 `BOT_ONEBOT_TOKEN` 相同 |
| 消息格式 | 数组 |

容器里的 `127.0.0.1` 指容器自身。跨容器或跨机器部署请按启动指南配置可访问地址，不要直接照抄同机地址。

### 4. 启动并验证

在项目目录的同一个终端中执行：

```bash
set -a
source .env
set +a
.venv/bin/qunbot --check
.venv/bin/qunbot
```

程序不会自动加载 `.env`。`--check` 检查本地配置，不会验证模型服务是否在线。

Bot 开始监听后，启用 NapCat 的连接，在白名单群里 `@机器人 你好`。确认日志收到消息、模型请求成功、QQ 群里出现回复。

WebUI 默认地址：**http://127.0.0.1:6200/**。使用 `BOT_WEBUI_TOKEN` 登录；在配置页保存修改后，重新加载环境并重启 Bot。

## 使用文档

| 你想做什么 | 文档或文件 |
| --- | --- |
| 安装、接入 QQ、启用语音与定时任务 | [完整启动指南](docs/QUICKSTART.md) |
| 调整回复频率、查看记忆、管理任务 | [WebUI 操作手册](qunbot/extensions/webui/docs/GUIDE.md) |
| 修改角色、人设和说话风格 | [人格文件](config/persona.md) |
| 查配置项、实现方式和功能限制 | [功能说明](docs/FEATURES.md) |
| 了解短期上下文与话题选择 | [对话理解设计](docs/CONVERSATION_INTELLIGENCE_DESIGN.md) |
| 查看模块缺口与后续开发计划 | [开发路线](docs/DEVELOPMENT_ROADMAP.md) |

## 扩展与开发

可选功能通过 `BOT_EXTENSIONS` 选择，并在本地 manifest 中注册。对话编排、存储、媒体、工具和任务有独立接口；新增一种任务动作不需要修改调度循环。

Skill 提供对话知识和能力使用说明。群成员不能安装代码、修改运行配置或执行任意脚本；维护者已启用的群管理动作则按各自权限和规则运行。**AstrBot 插件不能直接安装到本项目，需要移植适配。**

人格与常驻知识尽量保持稳定，记忆、心情和本轮线索按预算注入动态上下文，以减少重复输入的成本。实际缓存命中由模型服务商决定。

开发环境、包边界和提交要求见 [开发指南](CONTRIBUTING.md)。问题反馈请提交 [Issue](https://github.com/iceSheep2003/qq-bot/issues)，附复现步骤和脱敏日志，不要上传密钥或聊天数据库。

## 使用注意

- **数据与费用：** 群聊记录会写入本地数据库，部分内容会发送给模型或语音服务，并可能产生费用。部署前请让群成员知晓，限制白名单和数据访问。
- **权限与在线状态：** 群管理需要 QQ 账号具备对应权限；授予专属头衔通常需要群主权限。NapCat 登录失效或触发风控时可能需要重新扫码，Bot 不能保证永久在线。
- **接口兼容：** 图片理解、工具调用、语音与缓存统计取决于服务商支持。自动化测试通过不代表所有模型和 QQ 客户端组合都已实机验证。
- **未完成部分：** 知识图谱抽取默认关闭，尚未参与对话召回；高校照片与校徽轮换海报尚未完整接入。详见开发路线。

## 来源与许可

第三方表情图片需单独取得，仓库保留分类映射和来源说明；Kinna 素材随源码提供。项目整体许可证尚未指定，第三方内容按各自许可使用，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。

相关项目：[NapCatQQ](https://github.com/NapNeko/NapCatQQ) · [AstrBot](https://github.com/AstrBotDevs/AstrBot) · [狗头军师](https://github.com/shengjidaguai-china/goutoujunshi) · [AstrBot 表情图包](https://github.com/anka-afk/astrbot-meme-pack-official-01)
