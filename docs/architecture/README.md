# 当前项目结构

这是维护代码的目录契约。所有生产 Python 模块只有一个位置：`src/qqbot/`。
导入包名为 `qqbot`，产品名称是 Kinna bot。

## 模块归属

| 包 | 责任 | 主要入口 |
| --- | --- | --- |
| `agent` | 在线回合与 Agent 能力编排 | [service.py](../../src/qqbot/agent/service.py)、[orchestrator.py](../../src/qqbot/agent/orchestrator.py) |
| `actions` | 校验并执行工具、回复、原生互动和任务 | [tools/kernel.py](../../src/qqbot/actions/tools/kernel.py)、[replies/dispatcher.py](../../src/qqbot/actions/replies/dispatcher.py) |
| `features` | 固定清单中的可选内置功能 | [manifest.py](../../src/qqbot/features/manifest.py)、[loader.py](../../src/qqbot/features/loader.py) |
| `infrastructure` | 模型、QQ、数据库与具体工具处理器 | [adapters](../../src/qqbot/infrastructure/adapters)、[storage](../../src/qqbot/infrastructure/storage) |
| `interfaces` | 管理界面与维护查询 | [webui/server.py](../../src/qqbot/interfaces/webui/server.py) |
| `bootstrap` | 配置与具体实现的组合 | [app.py](../../src/qqbot/bootstrap/app.py)、[config.py](../../src/qqbot/bootstrap/config.py) |
| `contracts` | 公共模型、端口、协议与本体定义 | [models.py](../../src/qqbot/contracts/models.py)、[ports.py](../../src/qqbot/contracts/ports.py) |

以上链接相对于仓库根目录的源码位置；每个源码包的 README 说明内部归属。

Agent 内部按六种职责组织：`perception/` 处理输入理解，`conversation/` 处理场景、
工作记忆与压缩，`identity/` 保存自身规则与情绪，`memory/` 负责长期事实、证据和关系，
`cognition/` 选择可选认知路线，`skills/` 负责声明式 Skill 的可见性与加载。
`context.py` 管理动态上下文，`fairness.py` 管理后台工作的范围预算。

## 依赖规则

启动装配可以选择具体实现。Agent 主循环读取端口和数据模型，通过注入对象访问模型、
存储和发送能力。公共契约位于依赖底层，不能反向导入 Agent、动作或任何具体接入。

`.importlinter` 执行六条约束：公共契约、场景准入、Skill 范围、Skill 目录、认知决策、
Agent 主循环。它们禁止向具体基础设施、管理界面和功能装配反向依赖。
认知抽取中使用 Instructor 的具体适配器归到 `infrastructure/extraction/`。

内部功能注册仍通过固定的 `features/manifest.py` 完成。该包包含注册和运行逻辑，
部分旧记忆模块也仍有内部 I/O；这轮迁移没有把所有模块改造成纯函数或消除全部历史耦合。

## 项目文件

- `tests/` 按以上七种职责分组，`tests/quality/` 额外验证布局、资源分发与质量报告。
- `tools/experiments/` 是离线实验；`tools/operations/` 是手动运维；
  `tools/corpus/` 是语料处理；`tools/quality/` 是真实检查适配器。
- `skills/` 与 `config/` 是部署者声明式输入；`data/` 是本地运行状态。
- `docs/` 区分当前说明、使用指南、维护计划、设计参考、实验与历史。
- `.code-quality-gate/` 保存锁定标准与可追溯证据，`.quality/` 保存生成报告。

部署者的两个扩展入口是 Skill 与声明式定时任务。工具、图谱 schema、权限和数据库迁移
由开发者维护。`features/` 是内置代码，不能通过新增外部目录自动安装执行。

## 当前功能边界

默认使用 LLM 对话。群聊与私聊分别决定可选图谱模式，私聊不会继承群聊图谱配置。
不同 scope 的记忆隔离；跨场景授权共享与撤销、新图谱结构、私聊定时任务尚未实现。
运行时一致性与多群边界见 [RUNTIME.md](RUNTIME.md)，后续事项见
[开发路线](../development/ROADMAP.md)。

本次目录迁移没有改动真实 `.env`、数据库、人格或任务配置。旧 Python 内部路径已迁移，
维护脚本需使用新导入。正式启动命令为 `qqbot` 和 `kinna-bot`，以前的包路径与命令别名已移除。
