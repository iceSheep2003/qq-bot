# 决策图谱：工具选型与构建方法调研

> **已被取代**：本文件的结构设计作废，以 `KINNA_DECISION_ONTOLOGY.md` 为准。决策本体不建模群友个人。

状态：调研结论 + 待确认选型  
日期：2026-10-19  
结论先行：**主流"知识图谱工具"解决的不是我们要解决的问题。** 我们要的是"从日志里学一个决策策略"，这件事没有现成框架，必须自己搭；但可以借用其中几件小工具。

**更正（同日）**：初稿漏掉了这个项目**已经在用**的抽取栈——`instructor`。它就在 `pyproject.toml` 里，`infrastructure/adapters/structured.py` 是适配层，语义抽取和行为抽取都跑在它上面。选型应先从现有栈出发，而不是从外部候选清单出发。见第 3.5 节。

---

## 1. 调研范围

| 类别 | 候选 | 结论 |
|---|---|---|
| 嵌入式图数据库 | Kùzu、pyoxigraph | Kùzu 已归档；pyoxigraph 可用但偏 RDF |
| 服务型图数据库 | Neo4j、Memgraph、FalkorDB | 对单机 bot 是过度设计 |
| 嵌入式分析库 | SQLite（stdlib）、DuckDB | **推荐** |
| KG 构建框架 | Microsoft GraphRAG、LightRAG、fast-graphrag、Graphiti、neo4j-graphrag、LlamaIndex PropertyGraphIndex | 都是"文档→实体图→检索"，不是"日志→决策" |
| 模式与校验 | LinkML、jsonschema、pySHACL | LinkML 适合做模式定义，jsonschema 已在依赖里 |
| 信息抽取 | GLiNER / GLiNER2 / GLiREL、jieba、HanLP | 只服务内容层，不服务决策层 |
| 图分析 | networkx | 离线算社交指标，不进运行时 |

---

## 2. 关键事实（影响选型的两条）

### 2.1 Kùzu 已经归档，不能再作为长期依赖

- 仓库首页明确写着："We are archiving the KuzuDB project here"，最后提交 2025-10-10，推荐版本 v0.11.3。
- 据欧盟并购清单，Apple 已于 2025 年 10 月收购 Kùzu（约 10 人团队）。
- 现有版本仍可用，但官方扩展服务器已停止提供，扩展需自建镜像。

**判断**：作为内部实验可以，作为要跑一年的 bot 的地基不行。这条如果不查，我大概率会选它——它的特性（嵌入式、Cypher、向量+全文索引）对这个场景几乎完美。

### 2.2 这个仓库的依赖极其克制

`pyproject.toml` 只有 6 个运行时依赖：websockets、httpx、croniter、Markdown、**jsonschema**、instructor。  
venv 里没有 numpy、pandas、duckdb、networkx。

**判断**：运行时不能为了图谱引入任何新依赖。`sqlite3` 是标准库，`jsonschema` 已在依赖里——这两样已经够用。

---

## 3. 为什么主流 KG 工具不适用

| 工具 | 它解决的问题 | 与我们的差距 |
|---|---|---|
| Microsoft GraphRAG | 文档语料 → 实体图 + 社区摘要，回答"全局性问题" | 需要 LLM 逐块抽取，31k 消息成本高；产出是摘要，不是策略 |
| LightRAG / fast-graphrag | 同上，更便宜更快 | 同上 |
| Graphiti / Zep | 对话记忆的时序图，双时态边 | 解决"记住什么"，不解决"该不该出手"；且依赖 Neo4j/FalkorDB |
| neo4j-graphrag | Neo4j 官方的 KG 构建 + 检索封装 | 强制要求 Neo4j ≥ 5.18 服务在跑 |
| LlamaIndex PropertyGraphIndex | 图存储抽象 + LLM 抽取器 | 同样是文档抽取范式 |
| Kùzu | 嵌入式图存储 | 已归档 |
| pyoxigraph | 嵌入式 RDF/SPARQL | 仍是 Alpha（Development Status 3），RDF 三元组对"带权决策边"不自然 |

共同点：**它们都在解决"知识从哪里来、怎么检索"，而我们要解决的是"这个场合该不该我说话"。** 后者本质上是**行为克隆（behavior cloning）/ 上下文赌博机的策略估计**，属于离线策略学习，不是知识图谱构建。

把决策问题硬塞进 GraphRAG，会得到一堆实体和关系，然后发现没有一处能回答"要不要回"。

---

## 4. 选型矩阵

评分：● 合适　○ 可接受　× 不合适

| 方案 | 运行时依赖 | 需服务 | 确定性 | 成本 | 成熟度 | 适配 | 结论 |
|---|---|---|---|---|---|---|---|
| SQLite（stdlib） | 0 | 否 | 高 | 0 | 极稳 | ● | **运行时选它** |
| DuckDB | +1（构建期） | 否 | 高 | 0 | 稳 | ● | **构建期选它**（窗口函数算 heat/gap 极顺） |
| pyoxigraph | +1 | 否 | 高 | 0 | Alpha | ○ | 备用：将来要 SPARQL 再说 |
| Kùzu | +1 | 否 | 高 | 0 | **已归档** | × | 排除 |
| Neo4j | +驱动+服务 | **是** | 高 | 运维成本 | 极稳 | × | 单机 bot 不引入服务 |
| GraphRAG / LightRAG | 多 | 否 | 低（LLM） | 高 | 稳 | × | 范式不对 |
| Graphiti | +Neo4j/FalkorDB | 是 | 中 | 中 | 稳 | ○ | 只借鉴双时态思路 |
| LinkML | +1（构建期） | 否 | 高 | 0 | 稳（Apache-2.0） | ● | 模式定义可选 |
| jsonschema | 已有 | 否 | 高 | 0 | 稳 | ● | **校验选它** |
| jieba | +1（构建期） | 否 | 高 | 0 | 稳 | ● | 中文分词，可选 |
| GLiNER2 / GLiREL | +torch | 否 | 中 | 中 | 新 | ○ | 内容层抽取，二期再说 |
| networkx | +1（构建期） | 否 | 高 | 0 | 稳 | ● | 离线算社交指标 |

---

## 5. 推荐栈：构建期与运行期分开

### 构建期（离线，可以装东西）

```
原始语料（已有，脱敏）
  → 归一化 / 切分 / 打标        纯 Python（已有 tools/corpus）
  → 特征与聚合                  DuckDB（窗口函数算 heat、gap、rolling 计数）
  → 模式定义                    LinkML YAML（可选）→ JSON Schema
  → 校验                        jsonschema + 自定义不变式
  → 快照                        JSONL（可 diff）+ SQLite（可查询）
  → 指标                        networkx（社交图指标）、自写评估
```

### 运行期（bot 进程内，零新增依赖）

```
消息进来
  → 特征（纯 Python，8 维枚举）
  → SQLite 查决策表（一次主键/索引查询，< 5ms）
  → DecisionPacket
  → 门禁（频率、冷却、静默）
  → LLM 表达（或直接沉默）
```

**为什么不用图数据库**：运行期的访问模式是"桶 → 动作分布"的**键值查询**，不是多跳遍历。为键值查询引入图数据库，是拿运维复杂度换一个用不上的能力。真需要多跳时，SQLite 的递归 CTE 足够跑 3～5 跳。

---

## 6. 构建方法：七个步骤

这是"怎么构建"的答案。每一步都有明确的产物和验收，不是"跑个脚本看看"。

### 步骤 0：定义决策与标签口径

- 决策点 = 一条消息；
- 参与 = 180 秒内被引用回复，或被 @ 作者；
- 抽 100 条人工判定，算与规则的一致率（Cohen's κ ≥ 0.8 才继续）。

**当前数据实测**：31530 条消息，参与 19.7%，沉默 80.3%。

### 步骤 1：特征（只读 trigger 之前）

8 个维度，全部低基数枚举：situation / addressed / heat / gap / standing / novelty / media / hour。

**泄漏审计**：任何用到回复内容的特征一律删除；同一 thread 不跨训练测试。

### 步骤 2：分桶与回退

桶键 = 8 维拼接。稀疏是必然的，所以做三级回退：
`全维 → 去 hour/media → situation|addressed|heat → situation`。  
每级带 `support`，support 不足就降级，降级链本身进决策包。

### 步骤 3：概率估计要平滑，不要用频率硬算

小样本桶直接算 `engaged/total` 会得到 0 或 1。用 Dirichlet 平滑：

```
p = (engaged + α) / (total + 2α),  α = 1
```

置信度用 Wilson 下界，而不是样本量拍脑袋。这是决策表能不能信的关键——没有它，support=3 的桶会和 support=800 的桶一样自信。

### 步骤 4：阈值由代价决定，不是由 F1 决定

误接（打扰群友）和漏接（该接没接）的代价不一样。贝叶斯决策边界：

```
p* = C_fp / (C_fp + C_fn)
```

先给出这两个代价的比值（比如误接:漏接 = 3:1 → p* = 0.75），再在验证集上验证。**不要用 0.5 当默认阈值**——在 80% 沉默的数据上，0.5 等于"几乎全沉默"。

### 步骤 5：评估必须说清楚能证明什么

| 能证明 | 不能证明 |
|---|---|
| 行为克隆的准确率（和人类选择的一致度） | 策略上线后效果更好 |
| 校准度（预测 0.4 的桶里是否真有四成被接） | 用户更喜欢 |
| 覆盖率（多少消息能落到有统计的桶） | 成本真的降了（要实测调用量） |

**关键限制**：没有 logging policy 的 propensity，**IPS/DR 这类反事实评估做不了**。我们只有人类行为的观测，没有"当时如果换成 bot 会怎样"的数据。所以离线只能做行为克隆评估，真实收益必须靠影子模式。

### 步骤 6：运行时埋点（否则永远无法评估）

从第一天就记录每次决策：

```
{state, p, support, 是否发送, 发送后 180s 内是否有人回应, ε 探索标记}
```

以 ε=0.05 的概率随机探索（该沉默时偶尔接），**这是将来能做反事实评估的唯一前提**。不埋这个点，一年后还是只能算行为克隆准确率。

### 步骤 7：反例回流

LLM 拒绝执行的决策、误接被群友怼的案例、漏接后冷场的案例，全部回写数据集，按月重建。

---

## 7. 我之前的代码怎么处理

已经写在工作区的 `kg/` 下（schema/observe/signal/decide/content/constraint 六个模块），**没有进仓库**。

其中仍然成立的部分：L0 观测层、8 维信号、SILENCE 一等动作、时间切分评估。  
需要按本文改掉的部分：概率估计没有平滑、阈值没有按代价定、没有埋点设计、存储用了自造 JSONL 而没走 DuckDB 聚合。

**建议**：不在这份代码上继续长，按第 5、6 节重写构建期管线；运行期只保留一个 SQLite 读取器。

---

## 8. 需要你拍板的两件事

1. **存储**：运行期用 SQLite（零依赖，推荐），还是 pyoxigraph（要 SPARQL 时再换）？
2. **评估范围**：本期只做离线行为克隆评估，还是同时把运行时埋点（含 ε 探索）一起做进去？

这两条定了，我再动手写构建管线。

---

## 参考

- Kùzu 仓库归档公告：https://github.com/kuzudb/kuzu
- Apple 收购 Kùzu（欧盟并购清单报道）：https://cloud.tencent.com/developer/news/3582894
- Neo4j GraphRAG Python：https://neo4j.com/docs/neo4j-graphrag-python/current/
- pyoxigraph：https://pypi.org/project/pyoxigraph/
- LinkML：https://linkml.io/linkml/
- GLiNER2 论文：https://arxiv.org/pdf/2507.18546
