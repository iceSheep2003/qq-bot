# 决策图谱结构 v4

> **已被取代**：本文件的结构设计作废，以 `KINNA_DECISION_ONTOLOGY.md` 为准。决策本体不建模群友个人。

状态：结构设计（替代 v3 的四视图划分）  
一句话：**图不是知识库，是一张"状态 → 动作 → 结果"的决策表。** 知识只是动作的附件。

---

## 1. 为什么四视图的结构是错的

v2/v3 按"内容类型"切图：自我、语义、社会、行为。这个切法适合给人看，不适合给机器做决策，原因有三：

1. **决策被拆散了。** "要不要接这句话"需要同时读四个视图，而四个视图各有一套 id、各有一套更新节奏，运行时拼装成本高。
2. **沉默无法表达。** 视图里没有"什么都不做"的位置。可数据里 80.3% 的消息就是没人接——不把沉默建模成一等动作，图必然学成"每条都想回"。
3. **内容和决策混在一起。** 概念、黑话属于"要说什么"，参与、动作属于"要不要说、做什么"。混在一层，改内容就会影响决策，改决策又会碰到内容。

正确的切法是**按决策链分层**，每一层只依赖上一层，方向单向。

---

## 2. 五层结构

```
L0 观测层   原始消息、线程、人、群、回复关系       —— 不可变，事实源
     ↓ 确定性抽取（只读 trigger 之前的上下文）
L1 信号层   结构化特征向量（低基数、可枚举）        —— 不允许出现自由文本
     ↓ 分桶 + 回退
L2 决策层   状态 —动作→ 结果，含 SILENCE 动作      —— 图的主体
     ↓ 动作声明需要哪类内容
L3 内容层   概念、事实、黑话、资料                  —— 只被引用，不参与决策
L4 约束层   能力、边界、频率、冷却                  —— 门禁，不是知识
```

依赖方向只能向下：L2 可以引用 L3/L4，L3/L4 不能反向影响 L1。这条约束保证了"改内容不动决策"。

---

## 3. 各层存什么

### L0 观测层

| 节点/边 | 说明 |
|---|---|
| `Message` | 原话、时间、作者、群、回复目标、媒体段 |
| `Thread` | 会话切分（连续对话段） |
| `Actor` | 发言人（哈希 id，不存 QQ 号） |
| `Group` | 群 |
| `replies_to` / `part_of` / `authored` | 结构边 |

这一层只增不改。所有上层结论都必须能指回这里的 message id。

### L1 信号层

`SignalVector` 是决策的唯一输入，**固定 8 个维度**，全部可枚举：

| 维度 | 取值 | 来源 |
|---|---|---|
| `situation` | question / request_material / complaint / score_talk / plan / media / chitchat | trigger 文本 + 媒体段 |
| `addressed` | none / at_someone / in_thread | @ 段、引用段 |
| `heat` | cold / warm / hot | 前 10 分钟本线程消息数 |
| `gap` | burst / normal / slow | 距上一条消息的时间 |
| `standing` | new / regular / core | 作者历史发言量分位 |
| `novelty` | new_topic / ongoing | 与近 10 条消息的概念重合度 |
| `media` | none / image / file | trigger 段类型 |
| `hour` | day / night | 时间戳 |

硬规则：**信号层不允许存原文**。一旦把文本塞进信号，检索会退化成相似句匹配，图就又开始"替人说话"了。

### L2 决策层（图的主体）

| 节点/边 | 字段 | 说明 |
|---|---|---|
| `State` | `bucket`、`support`、`backoff_level` | 信号向量分桶后的状态 |
| `Action` | `key`、`kind`(silence/reply)、`act`、`target_rule`、`knowledge_need` | 动作空间 |
| `State→Action` 边 | `support`、`share`、`median_delay_s`、`continuation_rate` | 这张表就是"策略" |
| `DecisionCase` | `trigger_ref`、`context_refs`、`state_ref`、`observed_action_ref`、`outcome` | 一条被观测到的真实决策 |
| `Outcome` | `continued` / `resolved` / `dropped` / `uncertain` | 动作之后发生了什么 |

动作空间是有限的：

```
SILENCE
REPLY{ act ∈ 8, target_rule ∈ {trigger_author, thread_opener, other},
       knowledge_need ∈ {none, term, slang, fact, material} }
```

**SILENCE 是一等动作。** 每个 State 都有一条指向 SILENCE 的边，`share` 就是该状态下"没人接"的历史比例。运行时它是默认候选，其他动作必须先超过它才有资格被选中。

### L3 内容层

`Concept`、`Claim`、`SlangSense`、`Material`。只在 `Action.knowledge_need != none` 时被引用，存的是"可以说什么"，不含任何句式、语气、模板。

### L4 约束层

`Capability`（能做什么）、`Boundary`（不能做什么）、`RateLimit`（频率与冷却）。这些是门禁：动作通过 L2 选出后，还要过 L4 才能执行。

---

## 4. 数据集怎么变成这张图

一个决策点 = 一条 trigger 消息。标签全部来自"3 分钟内是否有人接它"：

| 观测 | 变成 |
|---|---|
| 无人接 | `SILENCE` 动作 |
| 有人引用回复 / @ 作者 | `REPLY` 动作（act 取回复的动作分类） |
| 回复带概念/黑话/资料 | `knowledge_need` |
| 回复后 3 条内的走向 | `Outcome` |

实测分布（3 群 31530 条消息）：参与 19.7%，**沉默 80.3%**。这个比例直接决定：图的默认输出是"别说"，LLM 调用量理论上可以砍掉八成。

去泄漏三条硬规则：
1. 特征只读 trigger 及其之前的上下文；
2. 同一线程不跨训练/测试；
3. 按时间切分（70/15/15），不随机。

---

## 5. 运行时怎么用

```
trigger → L1 信号 → L2 状态匹配（三级回退）→ 候选动作（含 SILENCE）
        → L4 门禁过滤 → DecisionPacket → LLM 表达
```

`DecisionPacket` 只有结构化字段，没有一句话：

```json
{
  "state": "question|none|hot|day",
  "engage": {"p": 0.41, "support": 812, "silence_share": 0.59},
  "act": {"value": "answer", "dist": {"answer": 0.62, "agreement": 0.18}},
  "target_rule": "trigger_author",
  "knowledge": {"need": "term", "refs": ["concept_..."]},
  "evidence": ["case_..."],
  "confidence": 0.91
}
```

决策分流：

| 条件 | 行为 | LLM 调用 |
|---|---|---|
| `p(REPLY) < 0.15` | 沉默 | 0 |
| `0.15 ≤ p < 0.45` | 决策包给 LLM，由它定夺 | 1 |
| `p ≥ 0.45` | 决策已定，LLM 只表达 | 1 |

---

## 6. 这张图能回答什么、不能回答什么

| 能 | 不能 |
|---|---|
| 这种场合该不该接 | 这句话具体怎么说 |
| 该接谁、用什么动作 | 用什么语气、像不像 Kinna |
| 要不要带概念/黑话 | 黑话怎么组织成句子 |
| 这个决策有多可信 | 这个决策会不会被用户喜欢 |

右边那列全是 LLM 和人格的事。**图一旦开始回答右边，就是设计回退。**

---

## 7. 评估

| 指标 | 目标 |
|---|---|
| engage F1（沉默为多数类） | ≥ 0.45，且必须优于全接/全沉默基线 |
| 沉默正确率 | ≥ 0.85 |
| act top-3 | ≥ 0.70 |
| 每千条消息 LLM 调用次数 | 相对全量下降 ≥ 60% |
| 状态覆盖率 | 测试集中能落到某状态的比例 ≥ 95% |

---

## 8. 与现有代码

| 层 | 现有 | 动作 |
|---|---|---|
| L0 | `contracts/ontology/v1.py` 的 Message/Person/Group | 复用 |
| L1 | 无 | 新增 `SignalVector` 契约 |
| L2 | `behavior-graph-compile-v4` 的 Situation/InteractionCase | 重构：加 State/Action/SILENCE，去掉表达字段 |
| L3 | Concept/Claim/SlangSense | 复用，去掉 ExpressionPattern/Strategy 的措辞部分 |
| L4 | `agent/cognition/routes.py` 的通道开关 | 扩展为门禁 |

---

## 9. 本期不做

- 不做文本生成、不做措辞模板、不把人格写进图；
- 不做向量检索（信号层已是结构化枚举，先不引入 embedding）；
- 不做动态本体（类型演进留到 L2 稳定之后）。
