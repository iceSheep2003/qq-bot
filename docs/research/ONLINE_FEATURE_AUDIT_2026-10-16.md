# Kinna bot 线上能力审计与重构取舍

审计日期：2026-10-16（Asia/Shanghai）  
线上实例：`i-2ze884ngsym9cm66sul2`  
线上目录：`/opt/qq-companion-bot`  
审计方式：只读检查，未修改线上代码、配置、数据库或服务。

## 结论

线上运行的仍是旧版 `qunbot`，不是当前本地 `qqbot` 分支。当前重构不能假设线上已经切换到新架构；每个功能都必须以线上配置、日志、数据库记录和实际调用为证据。

## 能力状态

| 能力 | 线上证据 | 本轮取舍 |
|---|---|---|
| 群聊 | systemd 服务运行，群消息与回复记录持续增长 | 保留，作为已验证主路径 |
| 私聊 | 配置为关闭 | 本地继续实现，线上不改配置 |
| LLM 长期记忆提炼 | `memory_extraction_runs` 有 181 条，86 次成功；日志有 `memory extract` | 保留并迁移为 Wiki 提炼来源 |
| 长期记忆召回 | 日志出现 `recall_memory`，数据库有 173 条 memories | 保留读取语义，重做为 Wiki Reader |
| 黑话学习 | `BOT_SLANG_ENABLED=true`；候选 141 条 | 保留成熟的词注入流程，Wiki 只做投影和长期归档 |
| 黑话人工审核 | `slang_reviews` 为 0 | 不把“允许未审核”带入新设计；新 Wiki 条目需要状态和来源 |
| 未审核黑话理解/模仿 | `BOT_SLANG_ALLOW_UNREVIEWED=true`、`ALLOW_IMITATION=true` | 保留线上事实，重构时作为显式策略，不默认扩大权限 |
| 行为图谱 | 未发现 `BOT_BEHAVIOR_*` 启用配置，线上代码也没有新模块 | 暂不重构为线上能力，保留本地可选实验 |
| embedding | 线上 embedding 非空记录为 0 | 删除运行时 embedding、向量检索、向量排序；旧数据不静默删除 |
| 图谱关系 | 数据库有 2 条 relations，但未发现线上读取路径 | 不把旧关系表直接当作新 Wiki 读取依据，先做兼容读取评估 |
| Prompt 缓存 | 线上旧版模型为 `deepseek-flash`，未取得 provider 缓存命中指标 | 本地保持稳定前缀和动态后缀分层；上线前必须验证 provider 指标 |

## 长期记忆迁移原则

线上已有的记忆和黑话数据不能直接删除。新设计采用追加式迁移：

```text
旧消息/旧记忆/黑话候选
        ↓
证据与候选记录
        ↓
群 Wiki、人物 Wiki、私聊 Wiki
        ↓
按当前消息选择的动态上下文
```

Wiki 版本发布后只增加一个新的 `published_version`，旧事实保留来源、时间和状态。迁移失败时，Agent 仍可回退到旧的只读记忆接口。

## Prompt 缓存约束

稳定前缀只允许包含：

- 人设和固定安全规则；
- 稳定的 Skill 目录；
- 稳定的工具定义和顺序；
- 不带时间戳、用户 ID、群 ID、Wiki 版本的固定协议。

以下内容必须放到动态后缀：

- 当前消息；
- 最近对话；
- Wiki 条目和黑话命中项；
- 好感度和人物关系；
- Wiki 版本号；
- 当前时间、事件 ID、请求 ID。

Wiki 候选可以即时落盘，但发布版按批次更新：默认 8 条候选或 10 分钟触发一次，人工纠错可以强制发布。发布只改变动态 Wiki 版本和本地检索缓存，不改变稳定前缀。

## 删除、保留、后置清单

### 保留并重构

- LLM 记忆提炼；
- 群/人物/私聊作用域隔离；
- 黑话实际词注入；
- 好感度和人物关系；
- 事实来源、置信度、提及策略和时间戳；
- 短期对话压缩与最近场景。

### 删除或停止运行时使用

- embedding provider；
- embedding 计算和向量排序；
- 未经证据验证的向量召回路径。

旧数据库中的向量字段和历史内容只保留兼容性，不再生成、不再查询、不再返回。

### 后置调研

- 行为经验图谱线上接入；
- 多用户自动学习和关系推断；
- 复杂双向图谱推理；
- 全文 Wiki 自动注入。

## 当前质量门禁状态

当前工作树仍有 `EMBEDDING-RETIREMENT-012` 和 `CORE-IO-POLICY-011-R2` 的未完成改动，不能把它们当成已完成重构。后续每项修改必须遵循：

1. 先建立 CQG 任务和失败测试；
2. 保留真实 red 证据；
3. 实现最小改动；
4. 运行专项测试；
5. 运行最终质量门禁；
6. 记录线上证据和未验证项。

## 本地重构进度（2026-10-16 后续）

- `WikiReader` 已完成：按群/人物/私聊隔离，默认只读取相关条目，按观察时间倒序选取；`full` 必须显式请求。
- `WikiWriter` 已完成：把已发布的 `WikiPage` 原子写成普通 Markdown，不依赖 Obsidian，不读取或写入线上数据库。
- `LegacyWikiProjector` 已完成：旧 `memories`、`memory_topics`、`slang_candidates` 行只读投影为群/人物/私聊 Wiki；候选记忆、无释义黑话和拒绝黑话不会进入发布页。
- `MarkdownWikiStore` 已完成：迁移生成的 Markdown 可以在重启后恢复为 Wiki 实体；SQLite 读取仍位于 infrastructure 层，Agent 不依赖具体存储。
- 可选 Wiki runtime 已完成：`BOT_WIKI_ENABLED=false` 时不读取、不创建缓存、不改变 Agent；线上当前没有该开关，保持关闭。
- Reader/Writer 尚未接入线上旧版 `qunbot` 的启动组合；线上仍保持原有长期记忆和黑话路径。
- 下一步是只读投影旧 `memories`、`memory_topics` 和黑话记录，生成可回滚的 Wiki 快照，再决定是否替换 Agent 读取路径。

本地迁移命令：

```bash
# 只读预览，不创建 Markdown
PYTHONPATH=src python -m qqbot.infrastructure.storage.wiki_migration \
  --db ./data/qqbot.sqlite3 --wiki-root ./data/wiki

# 明确要求后才写入 Wiki 快照；旧数据库仍以只读方式打开
PYTHONPATH=src python -m qqbot.infrastructure.storage.wiki_migration \
  --db ./data/qqbot.sqlite3 --wiki-root ./data/wiki --write
```

迁移输出包含页面数、事实数、主题数、黑话数和实际写入数；重复执行会生成相同内容。

