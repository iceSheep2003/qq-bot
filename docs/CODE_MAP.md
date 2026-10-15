# Kinna bot：代码从哪里看

实际仓库：`/Users/ice.sheep/Documents/Codex/2026-09-22/ni/outputs/qq-companion-bot`。
本轮重构所在分支：`codex/private-chat-dual-route`。
打开根目录的 `Kinna bot.code-workspace`，即可在 VS Code 中阅读整个仓库。
VS Code 左下角显示当前分支，源代码管理页可查看本轮修改。

## 建议阅读顺序

1. `qunbot/app.py`：启动与组装。把消息接入、模型、存储、群私聊策略装配起来。
2. `qunbot/conversation/channels.py`：判断群聊或私聊，校验会话身份与准入。
3. `qunbot/runtime/service.py`：消息进入后的处理、回复触发、发送和记录。
4. `qunbot/runtime/agent.py`：组织本轮上下文，使用当前场景的 Skill 和认知通道。
5. `qunbot/skills/audience.py`：群聊／私聊可见的 Skill 范围。
6. `qunbot/cognition/routes.py`：分别选择群聊与私聊的默认 LLM 或可选图谱通道。
7. `qunbot/harness/tools.py`：内置工具和执行权限，防止私聊调用群管理操作。

简单流程：QQ 消息 → 场景校验 → 会话服务 → Agent（Skill、记忆、可选认知通道）→ 模型 → 回复发送。
以上文件中，channels.py、audience.py 和 cognition/routes.py 是本轮新增的职责边界；
其余文件是已有模块的接入改造。运行时、工具和图谱由开发者维护。

## 功能扩展的入口

- `skills/*/SKILL.md`：声明式 Skill，描述能力与使用条件。群私聊范围由 audience 控制。
- `config/schedules.example.json`：声明式定时任务示例。

目标只保留 Skill 与声明式定时任务两个公开扩展入口。
旧版 extensions 目录目前仍承担内置功能装配，旧的扩展说明还需要后续收敛。

## 这次已经完成的范围

- 群私聊准入与会话范围校验。
- Skill 的群私聊可见性、选择与显式加载隔离。
- 群私聊分别启用图谱通道；私聊不会继承群聊图谱设置。
- 私聊工具权限检查，以及现有群聊行为的回归保护。
- 默认直接使用 LLM，可选图谱关闭时保持原有路径。

这只是第一阶段。跨场景记忆授权与撤销、新认知图谱结构及抽取、私聊业务 Skill、
私聊定时任务仍待开发；整个旧项目还没有完成迁移。现有私聊也尚未完成真实 QQ 联调。

## 验证与查看差异

- `tests/test_scene_refactor.py`：场景、Skill、通道、生命周期验收。
- `tests/test_scene_private_disabled.py`：私聊关闭时的边界。
- `tests/test_owner_skill_budget.py`：过滤无权使用的 Skill 后再应用选择数量限制。
- `tests/test_branding.py`：Kinna bot 名称与原有命令兼容。
- `.veritygate/ARCHITECTURE.md`：当前职责边界、质量要求与未完成范围。
- `.veritygate/tasks/` 与 `.veritygate/evidence/`：先测试后实现的任务和证据。

在 VS Code 终端查看第一阶段重构：

```sh
git diff b9b0af4..cafe738 -- qunbot tests
```

Python 导入包与启动命令仍为 `qunbot`，避免现有部署因产品改名而失效。
