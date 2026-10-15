# 行为图谱上线方案评估

评估时间：2026-10-15。目标：把行为经验图谱从"离线实验"变成线上可选策略。
线上实例：阿里云 ECS `i-2ze884ngsym9cm66sul2`（cn-beijing），部署目录 `/opt/qq-companion-bot`。

> **前提约定**：用户明确要求"不要动线上代码"。本文只是**评估**，不含任何已执行的上线动作。

---

## 1. 结论速览

| 问题 | 结论 |
| --- | --- |
| 资源够不够？ | **够**。图谱常驻实测 **88.5 MB**，线上 available **701 MB** |
| 改动大不大？ | **不大，但是一次"特性移植"**，不是加个开关。线上代码缺整个 `src/qunbot/agent/cognition/experience/` 模块 |
| 能不能只加图谱、不动其他代码？ | **能**。运行时依赖只有 `pydantic`（已装），**不需要 `instructor`** |
| 推荐做法 | **特性移植（7 个文件 + 4 处小补丁）**，而不是全量替换代码库 |
| 两条路由 | `BOT_BEHAVIOR_MODE=off / hint / graph`，默认 `off`（纯大模型），改配置即可切换 |
| 风险 | 中低。主要风险是 `agent.py` 补丁位置，以及线上无 git 导致的回滚依赖备份 |

---

## 2. 线上现状（2026-10-15 只读排查）

- 部署目录 `/opt/qq-companion-bot`，**不是 git 仓库**（从 Mac 拷贝，含 `._*` AppleDouble 文件）
- 代码快照停在 **2026-09-30**，**早于** `6f3fd13 固化分层架构重构`
- **`src/qunbot/agent/cognition/experience/` 目录不存在**；全代码搜 `behavior_experience` / `行为经验` → 0 命中
- `.env` 中 `BOT_BEHAVIOR_*` 配置 **0 条** → 图谱当前完全未启用
- 运行方式：systemd `qq-companion-bot.service`（root）
  ```
  ExecStart=/bin/bash -lc 'set -a; source /opt/qq-companion-bot/.env; set +a; exec /opt/qq-companion-bot/.venv/bin/qunbot'
  ```
- 模型 `deepseek-flash`；主群 `group:330707267`（1725 条消息，bot 回复 339 次）

### 线上缺失的新模块

本地 `qunbot/` 比线上多出：`contracts / conversation / harness / knowledge / mcp / ontology /
perception / skills / social / console / bootstrap / experience`。
这是分层架构重构的产物——**所以"全量替换代码"会顺带上线整个重构**，爆炸半径远大于图谱本身。

---

## 3. 实测资源占用

在本地对同一份快照实测（`data/private/derived/behavior-graph-compiled-final-20261014-v8`）：

| 指标 | 实测值 |
| --- | --- |
| 快照磁盘 | **12 MB**（nodes.jsonl 5.5M + edges.jsonl 6.9M + manifest 1.7K） |
| 加载后进程峰值 RSS | **123.1 MB** |
| 仅 import 的峰值 RSS | 34.6 MB |
| **图谱净增常驻** | **≈ 88.5 MB** |
| 加载耗时 | **321 ms** |
| 规模 | nodes 6930 · 路径 2007 · 词项 62109 |

对照线上：`available 701 MB`（`free` 只有 258M 是假象）、`swap` 1024M 已用 206M、
qunbot 自身 RSS 仅 34 MB。

**预算**：加载后 qunbot ≈ 34 + 88.5 ≈ **123 MB**，available 从 701 → **≈ 610 MB**。
**结论：内存可行**，但服务器上同时跑着 NapCat(287M)、一个 python(265M)、宝塔面板(93M)，
余量不算宽裕，上线后需要监控 RSS 与 swap。

---

## 4. 依赖分析：为什么可以"只移植图谱"

`src/qunbot/agent/cognition/experience/` 运行时只需要 **6 个文件**：

| 文件 | 行数 | 外部依赖 |
| --- | --- | --- |
| `__init__.py` | 19 | — |
| `models.py` | 82 | `pydantic`（**线上已装**） |
| `retrieval.py` | 112 | 仅标准库 |
| `compiler.py` | 132 | 仅标准库 |
| `graph_retrieval.py` | 378 | 仅标准库 + `.retrieval` |
| `runtime.py` | 100 | 仅标准库 + `.compiler` + `.graph_retrieval` |

**关键**：`extractor.py`（66 行，唯一 `from ..adapters.structured import InstructorOutput` 的文件）
是**离线抽取**专用，运行时路径完全不引用它。
线上没有 `adapters/structured.py`、也没有装 `instructor` —— **但这不影响上线图谱**。

只要**不拷贝 `extractor.py`**，就切断了 `instructor` / `contracts.schema` 依赖链。

---

## 5. 三条路径对比

| | A. 全量更新代码 | B. 特性移植（推荐） | C. 暂不上线 |
| --- | --- | --- | --- |
| 做法 | 把本地分支整体部署上去 | 只拷 `experience/` 6 文件 + 3 处补丁 | 维持现状 |
| 图谱可用 | ✅ | ✅ | ❌ |
| 顺带上线 | 分层重构 + 3.5 周全部改动 | 无 | — |
| 爆炸半径 | **大**（线上是生产群） | **小** | 无 |
| 回滚难度 | 高（目录级替换） | 低（删目录 + 撤补丁） | — |
| 长期维护 | 收敛，无分叉 | 线上与主线**分叉**，后续要合 | — |

**推荐 B**：图谱作为"可选策略"的定位，不值得为它上线整个重构；
而 A 的风险与图谱本身无关，属于"顺手把别的东西也换了"。

---

## 6. 路径 B 详细步骤（未执行，供评审）

> 全程建议在**业务低峰**（凌晨）执行，并先做完整备份。

**第 0 步：备份（必须）**
```bash
systemctl stop qq-companion-bot
cp -a /opt/qq-companion-bot /opt/qq-companion-bot.bak-$(date +%Y%m%d-%H%M%S)
cp -a /opt/qq-companion-bot/.env /opt/qq-companion-bot.env.bak
```

**第 1 步：上传图谱快照**（12 MB）
```bash
# 从本地（workbench CLI 支持 upload）
workbench upload --instance-id i-2ze884ngsym9cm66sul2 -r cn-beijing \
  --local data/private/derived/behavior-graph-compiled-final-20261014-v8 \
  --remote /opt/qq-companion-bot/data/private/derived/
# 可选：校准文件
workbench upload ... --local data/private/derived/behavior-validation-final-20261014/calibration.json \
  --remote /opt/qq-companion-bot/data/private/derived/behavior-validation-final-20261014/
```

**第 2 步：移植运行时 7 文件**（**不要拷 `extractor.py`**）

```bash
# 目标：/opt/qq-companion-bot/src/qunbot/agent/cognition/experience/
# 来源：__init__.py models.py retrieval.py compiler.py graph_retrieval.py
#       runtime.py decision.py
```

`decision.py` 是两条路由的抽象层（`BOT_BEHAVIOR_MODE`），依赖 `runtime.py`；
`runtime.py` 依赖 `compiler.py` 与 `graph_retrieval.py`。整条链只用到 `pydantic`。

**第 3 步：两条路由的补丁**（`BOT_BEHAVIOR_MODE` = off / hint / graph）

线上 `agent.py` 的接入点已核对过，行号如下（9/30 快照）：

1. `src/qunbot/agent/orchestrator.py`
   - `__init__`（@55）的关键字参数区加 `behavior_decision: BehaviorDecisionPort | None = None`，
     并存为 `self.behavior_decision`
   - `build_messages`（@158）里，在 `dynamic = {…}`（@242，含 `上下文信任级别` @263）
     **之后**插入钩子：
     ```python
     if self.behavior_decision is not None:
         parts = [event.text]
         if frame is not None:
             parts.extend(item.text for item in frame.recent_window[-8:] if item.text)
         try:
             decision = self.behavior_decision.decide("\n".join(parts))
         except Exception:
             log.exception("behavior decision failed; replying without it")
             decision = None
         if decision is not None:
             dynamic[decision.key] = {
                 "authority": decision.authority, "source": decision.source, **decision.block,
             }
     ```
2. `src/qunbot/contracts/ports.py`（**可选**）新增 `BehaviorDecisionPort` Protocol；线上没有也不影响，
   Python 是鸭子类型，直接传对象即可
3. `src/qunbot/bootstrap/app.py`（@160 的 `Agent(...)`）加一行：
   ```python
   behavior_decision=configured_behavior_decision(),
   ```
4. 新增两个模块：`src/qunbot/agent/cognition/experience/decision.py`、`src/qunbot/agent/cognition/experience/runtime.py`
   （`decision.py` 依赖 `runtime.py`；`runtime.py` 依赖 `compiler.py` + `graph_retrieval.py`）

> 线上没有 `src/qunbot/contracts/ports.py` 的 `BehaviorDecisionPort` 时，`agent.py` 的类型注解可以先用
> `Any` 或直接省略——运行时不检查注解。

**第 4 步：配置 `.env`**

```bash
# 选哪条路：off=纯大模型（默认） / hint=图谱给建议 / graph=图谱定调
BOT_BEHAVIOR_MODE=off
BOT_BEHAVIOR_GRAPH_PATH=/opt/qq-companion-bot/data/private/derived/behavior-graph-compiled-final-20261014-v8
BOT_BEHAVIOR_CALIBRATION_PATH=/opt/qq-companion-bot/data/private/derived/behavior-validation-final-20261014/calibration.json
BOT_BEHAVIOR_MAX_OPTIONS=3
BOT_BEHAVIOR_MAX_CASES=2
BOT_BEHAVIOR_CHAR_BUDGET=1200
BOT_BEHAVIOR_PRIOR_WEIGHT=off      # 默认关闭先验校准（见实验结论）
```

**先 `BOT_BEHAVIOR_MODE=off` 上线**（只验证代码没坏、内存没涨），确认无碍后再切 `hint`。

**第 5 步：启动前校验**
```bash
set -a; source /opt/qq-companion-bot/.env; set +a
/opt/qq-companion-bot/.venv/bin/qunbot --check
```

**第 6 步：启动并观察**
```bash
systemctl start qq-companion-bot
journalctl -u qq-companion-bot -f | grep -E "Behavior route|行为经验|本轮行为决策|model call"
# 期望：Behavior route: hint (snapshot /opt/...)
# 内存：ps -o rss= -p $(pgrep -f bin/qunbot)
```

**第 7 步：验证提示块真的进了 prompt**

- `hint` 模式：日志/请求里应出现 `行为经验`，且 `authority=derived_read_only`
- `graph` 模式：应出现 `本轮行为决策`，且 `authority=binding`

> ⚠️ 实测提醒：`graph`（binding）模式下，图谱对"最近压力好大想放弃"曾选出 **玩梗**——
> 这正是图谱 ~40% 准确率的体现。binding 规则里已写明「若与现场明显不符，以现场为准」，
> 但**上线时建议先只开 `hint`**，把 `graph` 留给能承受误判的场景。

---

## 7. 回滚方案

```bash
systemctl stop qq-companion-bot
rm -rf /opt/qq-companion-bot/src/qunbot/agent/cognition/experience          # 撤销第 2 步
# 还原第 3 步补丁（agent.py / ports.py / app.py）
cp -a /opt/qq-companion-bot.env.bak /opt/qq-companion-bot/.env
systemctl start qq-companion-bot
```

**更快的"软回滚"**：把 `.env` 里的 `BOT_BEHAVIOR_GRAPH_PATH` 清空即可——
`configured_behavior_experience()` 返回 `None`，Agent 不注入提示块，**不加载图谱、不占内存**。
这是设计上的保险：图谱是可选读模型，关掉它不需要改代码。

---

## 8. 风险与监控

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 内存不足 | OOM 杀进程，bot 掉线 | available 610M 余量；上线后监控 RSS + swap；必要时升配 |
| `agent.py` 补丁位置错 | 启动失败或提示块不注入 | 启动前 `--check`；补丁后先本地跑测试 |
| 线上无 git | 无法 `git checkout` 回滚 | 第 0 步目录级备份 + 软回滚开关 |
| 提示块质量差 | 拉低回复质量 | `BOT_BEHAVIOR_PRIOR_WEIGHT=off`；先只在一个测试群开 |
| 与主线分叉 | 后续合并成本 | 移植内容记录在案，时机成熟切到路径 A 收敛 |

---

## 9. 建议的推进顺序

1. **先不上线**。图谱目前 Top-1 29.3% / 平衡准确率 17.0%，天花板 40.9%——
   作为"可选策略"还不够格（见 `docs/research/BEHAVIOR_GRAPH_EXPERIMENTS.md`）。
2. **先修标签层**（下一步工作的重点），把上限抬起来。
3. 图谱质量可接受后，再按本文路径 B 上线，且**先在测试群**验证。

这与产品决策一致：LLM 决策保持默认，图谱作为可选档位，**质量达标再上**。
