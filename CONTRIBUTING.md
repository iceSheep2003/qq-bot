# Kinna bot 开发约定

当前目录归属以 [项目结构](docs/architecture/README.md) 为准，待做事项见
[开发路线](docs/development/ROADMAP.md)。先确定改动的职责、输入输出和验收条件，再写代码。

## 开发环境

从项目根目录执行：

```sh
sh .veritygate/ci-setup.sh
.venv/bin/python -m pytest tests -q
```

脚本安装锁定的 Python 依赖、可编辑源码与本地 VerityGate 工具链。
修改安装配置后重新执行 `.venv/bin/python -m pip install --no-deps -e .`。
生产命令 `qunbot` 与 `kinna-bot` 指向同一个启动入口。

自动化回归使用临时数据库和替身模型，不连接真实 QQ 或收费模型。
手动联调工具在 `tools/operations/`，离线实验在 `tools/experiments/`，
通过 `python -m tools.<分类>.<模块>` 调用；不要修改 Python 的搜索路径来补救错误导入。

## 新代码放在哪里

| 改动 | 归属 |
| --- | --- |
| 回合生命周期、上下文组装和模型编排 | `src/qunbot/agent/service.py`、`agent/orchestrator.py` |
| 场景准入、对话窗口与压缩 | `src/qunbot/agent/conversation/` |
| 记忆提炼、召回、证据和关系 | `src/qunbot/agent/memory/` |
| 可选认知通道与行为经验计算 | `src/qunbot/agent/cognition/` |
| 身份规则与 Bot 自身情绪 | `src/qunbot/agent/identity/` |
| Skill 目录、选择、加载和群私聊可见性 | `src/qunbot/agent/skills/` |
| 工具、发送计划、原生互动与任务执行 | `src/qunbot/actions/` 对应包 |
| QQ、模型、数据库等具体技术实现 | `src/qunbot/infrastructure/` |
| 管理界面或管理查询 | `src/qunbot/interfaces/` |
| 内置可选功能及其装配清单 | `src/qunbot/features/` |
| 核心配置和依赖装配 | `src/qunbot/bootstrap/` |
| 共享数据结构、端口、协议与 schema | `src/qunbot/contracts/` |

测试按职责放入 `tests/` 的对应目录，跨多个模块的场景回归放在 `tests/agent/`。
不要新增含义不明的 `common`、`utils`、`manager` 大包，也不要在仓库根部复制源码。

## 公开扩展与内部开发

部署者新增内容的入口是 Skill 与声明式定时任务。Skill 是数据和指令，任务定义是
对已有动作的排期；两者都不能注册 Python、工具权限、图谱类型或数据库迁移。

`features/manifest.py` 是开发者维护的固定内置清单，`BOT_EXTENSIONS` 选择其中的功能。
修改内置功能需要经过同样的代码审查和质量门禁。没有清单声明的包不会被扫描加载。
新增工具、schema、权限或任务执行器属于内部版本开发，不提供用户插件入口。

## 每次修改的质量流程

1. 写清需求、模块责任、输入输出、边界与验收条件。涉及目录迁移时列出旧新路径。
2. 先写验收测试，创建 `.veritygate/tasks/<任务编号>.json`，列明测试、实现和受保护文件。
3. 执行真实红阶段，确认是预期断言失败。导入错误、跳过和工具失败不能代替红阶段。
4. 冻结测试与质量标准，再实现代码。保持完整回归、架构约束和已有覆盖率门槛。
5. 运行绿阶段，修复实际问题；通过后再执行最终门禁。
6. 提交实现、任务与证据。只有最终门禁通过后才允许合并；发布和推送按用户授权执行。

```sh
node .veritygate/toolchain/node_modules/veritygate/src/cli.mjs workflow --change .veritygate/tasks/<任务编号>.json --phase red
node .veritygate/toolchain/node_modules/veritygate/src/cli.mjs workflow --change .veritygate/tasks/<任务编号>.json --phase green
node .veritygate/toolchain/node_modules/veritygate/src/cli.mjs gate
```

详细规则见 [.agents/skills/veritygate/SKILL.md](.agents/skills/veritygate/SKILL.md)。
最终报告在 `.quality/veritygate/`。旧任务证据属于各自的 Git 快照，不应为目录迁移重写。
确需修订已冻结的测试输入时，保留原输入与证据、说明原因并重新记录红绿过程。

## 依赖、记忆与发送

- 公共契约不能导入运行时实现。Agent 主循环通过端口使用模型和存储。
- 场景准入、Skill、认知决策不能导入具体平台、界面或功能装配。
- 基本人格与常驻知识放稳定前缀；当前话题、相关记忆和本轮状态进入有预算的动态上下文。
- 群聊和私聊按 scope 隔离。身份相同不构成共享个人记忆的授权。
- 区分生成、尝试发送和成功发送。只有成功回执对应的内容可记为已经说过。
- 迁移持久化字段要兼容旧数据库，覆盖重复执行、重启去重和跨场景隔离。

六条依赖约束由 `.importlinter` 执行。项目布局验收还会实际构建、安装 wheel，
从仓库之外导入全部模块、读取资源并验证两个启动命令。

## 文档与本地资料

`docs/architecture/` 记录当前结构，`docs/guides/` 记录使用方法，
`docs/development/` 记录维护事项；`docs/design/`、`docs/research/` 和 `docs/history/`
分别保存设计参考、实验与历史。不要把未完成设计写成已实现功能。

用户的讲解稿、课件和临时演示资料单独保存在仓库外的本地交付目录，不进入 Git。
不要提交 `.env`、真实任务配置、聊天数据库、用户档案、运行日志或第三方本地媒体。
来源与许可证要求见 [第三方说明](THIRD_PARTY_NOTICES.md)。
