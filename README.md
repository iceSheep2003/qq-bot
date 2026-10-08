# Kinna · QQ Bot

一个住在 QQ 群里的聊天 Bot。接话、看热闹、发表情，慢慢认识群里的每个人。

![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![OneBot v11](https://img.shields.io/badge/OneBot-v11-2563EB)
![NapCat](https://img.shields.io/badge/QQ-NapCat-2563EB)
![SQLite](https://img.shields.io/badge/Storage-SQLite-003B57?logo=sqlite&logoColor=white)

[快速开始](#快速开始) · [操作手册](qunbot/extensions/webui/docs/GUIDE.md) · [功能说明](docs/FEATURES.md) · [参与开发](CONTRIBUTING.md)

![Kinna：蓝发、黑卫衣，拿着一张清单](memes/ip/zinxtick-present-sheet.png)

Kinna 是随项目提供的人设，也可以换成你自己的角色。她没有必须经营的话题：群里聊游戏、熬夜、感情或者技术，都从眼前的聊天接起。

项目用 Python 开发，通过 NapCat 的 OneBot v11 反向 WebSocket 接入 QQ，通过 OpenAI 兼容接口调用语言模型。人格放在文件里，功能通过本地扩展注册；不需要把所有能力写进一段越来越长的提示词。

## 她会做什么

- **接得上眼前的话。** 回复前读取连续的群聊窗口，结合发言人、引用和最近互动判断要接哪一句；热聊时压缩较早的对话，长期 topic 只作背景。
- **认识群友。** 长期记忆保存事实、来源和修订记录；每个群、每个人有独立的关系状态与好感度。召回支持中文词法检索和可选的向量检索。
- **有自己的说话习惯。** 基础人格、临时表达倾向和心情共同影响回复。可以学习群里的黑话与表达方式，保留来源；自动更新对话示例时只改指定区域，不重写基本人设。
- **选一种合适的回复方式。** 回复 planner 统一处理文字、表情包、QQ 表情和语音。长文字由代码拆条；语音优先使用 15 字以内的短句，不把同一句话再发一遍文字。
- **参与 QQ 的小互动。** 支持引用回复、消息贴表情、戳一戳与跟风戳。程序复读只在出现 `+1` 时考虑，并持久化去重记录。
- **偶尔主动说话。** 群聊中的主动接话、安静后的续聊、部署者设定的定时发言是不同的路径，各自有概率、冷却或额度控制。
- **按权限做群管理。** 可选禁言、精华消息、群文件引导、入群审核、个性化欢迎和头衔提议。头衔需要本人同意或群内投票通过，还要满足 QQ 平台权限。
- **由维护者管理。** 蓝色侧栏 WebUI 提供配置、记忆浏览、关系图谱和定时任务管理，操作手册在独立的 Guide 栏目。聊天参与者不能安装扩展、修改运行配置或执行任意代码。

这些能力需要分别启用，部分还会产生额外模型调用。程序复读的限制也不等于模型绝不会重复措辞；聊天风格和连贯性仍需要通过真实对话回放调整。

## 快速开始

需要 Python 3.11+、已登录的 NapCat，以及一个可用的 OpenAI 兼容模型接口。当前版本依赖语言模型，不是离线陪聊引擎；仓库不提供 API Key 或公共中转站。

### 1. 安装

下载仓库后，在项目根目录执行：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp .env.example .env
cp config/schedules.example.json config/schedules.json
```

### 2. 配置

编辑 `.env`，至少填写：

```dotenv
BOT_MODEL_BASE_URL=https://your-provider.example/v1
BOT_MODEL_API_KEY=your-model-api-key
BOT_MODEL_NAME=your-chat-model
BOT_ONEBOT_TOKEN=your-long-random-onebot-token
BOT_GROUP_ALLOWLIST=123456789
BOT_PRIVATE_ENABLED=false
BOT_WEBUI_TOKEN=your-separate-long-random-webui-token
```

地址和模型名以你使用的服务商为准。OneBot 和 WebUI 使用两份独立的 Token，不要复用模型密钥。

示例配置启用表情包、情绪、人格、黑话、风格学习和 WebUI；**默认只在被 @ 时回复**。先跑通这一条链路，再开启主动接话、语音和定时任务。扩展的设置方法见 [完整启动指南](docs/QUICKSTART.md)。

### 3. 连接 NapCat

在 NapCat WebUI 的网络配置中新增 **WebSocket 客户端**：

| 字段 | 同机部署的值 |
| --- | --- |
| URL | `ws://127.0.0.1:6199/ws` |
| Token | 与 `BOT_ONEBOT_TOKEN` 一致 |
| 消息格式 | 数组 |

如果 NapCat 在容器里，`127.0.0.1` 指的是该容器，不是宿主机或 Bot 容器。跨容器、跨机器的配置见启动指南，别直接把管理端口暴露到公网。

### 4. 启动并验证

程序不会自动读取 `.env`。在同一个终端中加载配置，再检查、启动：

```bash
set -a
source .env
set +a
.venv/bin/qunbot --check
.venv/bin/qunbot
```

看到反向 WebSocket 的监听日志后启用 NapCat 连接。在白名单测试群里 `@机器人 你好`，确认收消息、模型返回和 QQ 发送三个环节都成功。

WebUI 地址为 `http://127.0.0.1:6200/`，使用 `BOT_WEBUI_TOKEN` 登录。配置页保存的是 `.env`，修改后需要重新加载配置并重启；记忆浏览和任务管理使用各自的接口，不要把配置保存当成所有功能的实时开关。

## 文档

| 你想做什么 | 从这里开始 |
| --- | --- |
| 安装、联网、启用语音或定时发言 | [完整启动指南](docs/QUICKSTART.md) |
| 在 WebUI 调参数、查看记忆和管理任务 | [操作手册](qunbot/extensions/webui/docs/GUIDE.md) |
| 查看每个功能的实现、配置和限制 | [功能说明](docs/FEATURES.md) |
| 理解短期窗口、话题和回复上下文 | [对话理解设计](docs/CONVERSATION_INTELLIGENCE_DESIGN.md) |
| 接手一个模块，补完尚未交付的部分 | [模块现状与开发路线](docs/DEVELOPMENT_ROADMAP.md) |
| 新增工具、扩展或 Skill | [开发约定](CONTRIBUTING.md) |
| 使用第三方 Skill 和表情素材 | [第三方来源与许可](THIRD_PARTY_NOTICES.md) |

## 代码怎么分

核心负责接收消息、组装上下文和调用模型；扩展负责自己的业务。工具、媒体和任务分别注册，不塞进会话对象。

```text
qunbot/
  adapters/                  QQ 和模型接口适配
  runtime/                   对话编排、上下文与工具权限
  conversation_intelligence/ 当前聊天窗口与接话线索
  conversation_compaction/   较早对话的模型压缩
  memory/                    长期记忆的提炼、召回与生命周期
  relationships/             按群、按人的关系状态
  emotion/                   Bot 自己的心情与衰减
  replies/                   回复规划、分段与媒体选择
  social_interactions/       +1、贴表情、戳一戳
  scheduling/                排期、任务注册与运行记录
  extensions/                人格、语音、群管理、WebUI 等可选包
  storage/                   SQLite 存储与迁移
config/persona.md             基本人设和可演进的示例区域
skills/                      本地指令与对话知识
tests/                       回归测试
```

扩展必须在本地 manifest 中声明贡献和权限，再由 `BOT_EXTENSIONS` 选择。Skill 提供对话知识或能力使用说明，不会因为群友一句话就安装代码、执行脚本。维护者有意配置的群管理能力仍可以接收符合规则的群内请求。

人格和 Skill 的常驻部分尽量保持稳定，记忆、心情与本轮线索按预算注入动态上下文。日志记录前缀摘要和服务商返回的用量；**前缀稳定不代表一定命中缓存**，实际命中取决于模型服务商。

## 使用前请留意

- 群聊记录会进入本地数据库，部分对话会发送给模型或语音服务。投入使用前，请让群成员知晓，并限制白名单、数据访问和保留范围。
- 视觉、工具调用、缓存统计和语音格式取决于服务商。`--check` 只检查本地配置，不证明模型服务可用；自动化测试通过也不等于 QQ 实机兼容性已验证。
- NapCat 掉线不一定是 Bot 故障。仓库附有一次性恢复尝试的 watchdog，但登录票据失效或触发风控后仍可能需要扫码，不能保证永久在线。
- 群管理需要 QQ 账号有对应权限，授予专属头衔通常需要群主权限。QQ 官方频道接入是独立扩展，需要官方机器人凭据，不能拿普通 QQ 号直接替代。
- 知识图谱抽取默认关闭，目前不参与模型召回；高校图片、校徽轮换的海报目录还未完整接入。不要把这两项当成已经完成的聊天能力。
- 第三方表情图片没有随此源码快照一起提交。目录映射和来源说明保留，自己的 Kinna 素材可直接使用；补充图包前先确认授权。

## 参考与致谢

- [NapCatQQ](https://github.com/NapNeko/NapCatQQ)：QQ 接入。
- [AstrBot](https://github.com/AstrBotDevs/AstrBot)：功能拆分与插件设计的参考。
- [狗头军师](https://github.com/shengjidaguai-china/goutoujunshi)：对话知识与群聊轻量适配，保留原版文件和 MIT 许可。
- [AstrBot 表情图包](https://github.com/anka-afk/astrbot-meme-pack-official-01)：可选的表情分类与素材来源。

项目整体许可证尚未指定。第三方内容按各自的许可证和授权范围使用，详见 [第三方说明](THIRD_PARTY_NOTICES.md)。
