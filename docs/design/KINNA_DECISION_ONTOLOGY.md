# Kinna 决策本体（KDO）v1

状态：本体设计稿（从零构建，取代此前的四视图 / 五层结构）  
方法：Ontology Development 101 七步法 + 能力问题驱动  
硬约束：**本本体只负责决策；人与人之间的交互与好感度是另一个模块，不归这里。**

---

## 0. 范围声明（先划边界，再谈建模）

### 本本体负责

- 判断"这条消息在当前情境下，Agent 该不该接、以什么行为接"；
- 记录判断依据的原话证据；
- 群级词汇与话题（这个词在群里是什么意思、最近在聊什么）；
- Agent 自身的能力与边界。

### 不归本本体负责，但**不等于不存在**

| 能力 | 归属模块 | KDO 与它的关系 |
|---|---|---|
| 好感度、关系阶段、互动评分 | `agent/memory/relationships`（好感度数据的唯一所有者） | **只读引用**，不计算、不存储、不写回 |
| 个人事实（昵称、名片、首见时间） | `relationships` 的 `group_members` | 不引用 |
| 情绪状态 | `features/mood` + `emotion.evaluator` | 本期不引用 |
| 人格与说话风格 | `config/persona.md`（prompt） | 不引用，不进图 |
| `SocialState` 投影 | `agent/memory/social/projection.py`（视图，非写入方） | 通过端口读视图，不直接读表 |

**这不是"把人的建模砍掉"**：那些模块继续存在、继续维护，只是**本体的职责边界**不包含它们。KDO 的类表里没有 `Person`/`Profile`/`Relationship`，是因为它们属于另一个模块，不是因为这个系统不做这件事。

### 接入方式：只读端口，不是复制，也不是删除

KDO 只通过一个**有界的只读视图**消费关系模块的状态：

```
SocialContextView
  affinityBand      unknown | low | neutral | high     # 分档，不给原始分数
  familiarityBand   unknown | low | medium | high
  stage             关系阶段枚举（由关系模块定义）
  observedAt        该视图的观测时间
  sourceModule      "agent/memory/relationships"
  viewVersion       视图版本
```

四条硬规则：

1. **只读**：KDO 永不写回关系模块，永不创建第二份好感度数据。
2. **只读分档，不读分数**：原始分值是关系模块的内部状态，决策只需要"偏高/偏低"这种粒度。
3. **不按人建档案**：`SocialContextView` 挂在**情境**上（这条消息涉及的那一段关系），不挂在 `Participant` 上，因此无法累积成用户画像。
4. **可追溯**：每个用到该视图的决策都记 `viewVersion` 与 `observedAt`，关系模块改版后能重放。

`Participant` 仍然保留，但只作为**引用锚点**：只有哈希 ID，作用仅限于"这条消息是谁发的""@ 了谁"。它不是人，是图上的一个坐标。

### 仍明确排除

| 排除项 | 原因 |
|---|---|
| 跨群人物识别 | 隐私风险最高，关系模块也不提供跨群视图 |
| 以人为主语的事实断言（"某人喜欢 X"） | 属于个人记忆能力，不在决策本体里 |
| 情绪对决策的影响 | 本期不接入，等关系/情绪模块稳定后再评估 |

---

## 1. 方法论

| 步骤 | 本项目做法 | 产物 |
|---|---|---|
| 1 定领域与范围 | 领域＝群聊参与决策；范围见第 0 节 | 11 条能力问题 |
| 2 复用本体 | SIOC / PROV-O / OWL-Time / SKOS / DiAML / DMN | 复用清单（第 3 节） |
| 3 枚举术语 | 从语料、标准、旧代码收集 | 术语表 |
| 4 类层次 | 自顶向下搭主干 | 第 5 节 |
| 5 属性 | 对象属性 + 数据属性 + 逆属性 | 第 6 节 |
| 6 facets | 值域、基数、互斥 | OWL 公理 + SHACL |
| 7 实例 | 构建管线从语料生成 | 数据快照 |

第 4、5 步交织推进；每次加类必须能指出"哪条能力问题需要它"，指不出来就不加。

---

## 2. 能力问题

| 编号 | 问题 | 模块 |
|---|---|---|
| CQ1 | 这条消息处在什么情境？情境由哪些可观察特征构成？ | Situation |
| CQ2 | 同类情境历史上有多大概率有人接话？样本量多少？ | Decision |
| CQ3 | 如果接，历史上最常出现的对话行为是什么？ | Decision |
| CQ4 | 这个判断依据了哪些案例、哪些原话？ | Provenance |
| CQ5 | 这条消息是在对谁说话？是否点名？是否是回复？ | Discourse |
| CQ6 | 这个群里某个词是什么意思？在哪条消息里被解释过？ | Lexical |
| CQ7 | 这个决策的置信度多少？来自哪个策略版本？ | Decision / Policy |
| CQ8 | 沉默与发言的历史比例是多少？ | Decision |
| CQ9 | 哪些硬约束会否决这个决策？ | Constraint |
| CQ10 | 这批数据由哪个抽取器、哪份语料、哪个本体版本产生？ | Provenance |
| CQ11 | 这次决策用了哪个版本的关系视图？视图观测于何时？ | Provenance / Situation |

注意：没有任何一条能力问题要求"描述某个人"。CQ11 问的是**决策用了哪份外部视图**，不是"这个人怎么样"——这是边界能守住的关键差别。

---

## 3. 复用的标准词表

| 词表 | 用途 | 采用 | 不采用 |
|---|---|---|---|
| **SIOC** | 在线讨论结构：Post / Thread / Forum / UserAccount，`has_reply`、`mentions`、`addressed_to`、`num_replies` | Discourse 模块的父类 | 站点、订阅、版本管理 |
| **PROV-O** | 来源链：Entity / Activity / Agent，`wasGeneratedBy`、`wasDerivedFrom`、`wasAttributedTo` | Provenance 模块骨架 | 复杂责任链推理 |
| **OWL-Time** | 时刻与区间 | `time:Instant` / `time:Interval` | 历法推理 |
| **SKOS** | 受控词表：情境类型、对话行为、词义 | `ConceptScheme` + `Concept` | 概念推理规则 |
| **ISO 24617-2 (DiAML)** | 对话行为分类 | 取"交际功能"上位类作参照 | 完整多维标注体系 |
| **DMN** | 决策表语义：决策、输入、输出、命中策略 | 借语义做决策表 | 图形符号与执行引擎 |
| **SHACL** | 闭世界校验 | 每个类一个 Shape | — |

为什么用 DiAML 而不是自编动作：自编词表无法与任何标注数据对齐，也无法解释"为什么是这 8 类"。  
为什么借 DMN：我们的"情境 → 动作"表在结构上就是一张决策表（输入项＝特征，输出项＝动作，命中策略＝首命中）。借它的语义，能直接复用完整性、一致性、无重叠这套检查。

---

## 4. 五个关键建模决策

### 4.1 决策要具体化，不能做成一条边

一次决策是六元关系：情境、候选动作、选定动作、依据、置信度、时间。做成 `Situation --decided--> Action` 就没地方挂置信度和样本量，也回答不了 CQ2/CQ7。

```
DecisionPoint ─hasSituation──▶ Situation
              ─hasCandidate──▶ CandidateAction
              ─chosenAction─▶ CandidateAction
              ─basedOn──────▶ Evidence
              ─hasConfidence▶ Confidence
```

### 4.2 开世界与闭世界分开

OWL 开世界：没写不等于不存在。我们的数据是闭世界的：没证据就是没证据。  
**OWL 只定义语义，校验一律用 SHACL。** 别指望推理器替你检查"每条断言必须有证据"。

### 4.3 受控词表用 SKOS，不建 OWL 类

`situationKind=question` 这类值如果每个都建成 OWL 类，类层次会爆炸。  
做法：每个维度一个 `skos:ConceptScheme`，值作为 `skos:Concept`，上下位用 `skos:broader`。本体只定义"有这个维度"。

### 4.4 双时态

`eventTime`（发生时间）与 `recordedAt`（记录时间）分开。语料是回溯导入的，两者差几个月；混在一起会拿未来信息评估过去决策。

### 4.5 沉默是一等动作

`Silence` 和 `Reply` 同为 `CandidateAction` 的子类。不把沉默建模成"没有动作"，否则统计上无法表达 80% 的实际情况，决策表会天然偏向发言。

---

## 5. 类

### M1 Discourse（复用 SIOC，只存结构）

| 类 | 父类 | 说明 | CQ |
|---|---|---|---|
| `Group` | `sioc:Forum` | 群 | CQ5 |
| `Participant` | `sioc:UserAccount` | **引用锚点**：只有哈希 ID，不承载任何画像属性 | CQ4 |
| `SocialContextView` | — | 关系模块投影出的**只读分档视图**，挂在情境上 | CQ11 |
| `Message` | `sioc:Post` | 消息；含 `hasAuthor`、段结构 | CQ4 |
| `Thread` | `sioc:Thread` | 连续对话段 | CQ1 |
| `ReplyLink` | — | 具体化的回复关系 | CQ5 |

属性：`sioc:has_reply`、`sioc:reply_of`、`sioc:addressed_to`、`sioc:mentions`、`sioc:has_creator`、`sioc:num_replies`。

`addressed_to` / `mentions` 的取值也是 `Participant`，但**只用于判断"这句话是不是在叫某个人"，不产生任何关于那个人的推论**。

`SocialContextView` 由 `agent/memory/relationships` 提供，字段见第 0 节。它挂在 `Situation` 上而不是 `Participant` 上——这条挂载位置决定了它无法被累积成用户档案。

### M2 Situation

| 类 | 说明 | CQ |
|---|---|---|
| `Situation` | 具体化情境：时间区间 + 触发消息 + 特征集 | CQ1 |
| `ContextWindow` | 有界、有序的消息序列（只含触发之前） | CQ1 |
| `FeatureValue` | 维度 + 取值 | CQ1 |
| `FeatureDimension` | 维度定义（8 个，见下） | CQ1 |

**七个内生特征 + 一个外部特征**。内生特征只描述情境，不描述人；外部特征由关系模块提供，只读、分档：

| 维度 | 取值 | 来源 | 说明 |
|---|---|---|---|
| `situationKind` | question / materialRequest / complaint / scoreTalk / planning / mediaShare / chitchat | 内生 | 这句话的性质 |
| `addressing` | none / atSomeone / inReplyThread | 内生 | 这句话对谁说，不是那人是谁 |
| `threadHeat` | cold / warm / hot | 内生 | 这段对话的密度 |
| `gap` | burst / normal / slow | 内生 | 两条消息的间隔 |
| `novelty` | newTopic / ongoing | 内生 | 与近期话题的重合度 |
| `mediaKind` | none / image / file | 内生 | 这条消息的形态 |
| `hourBucket` | day / night | 内生 | 时间 |
| `socialBand` | unknown / low / neutral / high | **外部（关系模块）** | 只取分档，不取分数；缺省 `unknown`，缺省时决策照常工作 |

两条纪律：

- `socialBand` 是**可选输入**。关系模块不可用时降级为 `unknown`，决策表里有对应的桶，不允许因为缺它而拒绝决策。
- `socialBand` 的来源是 `SocialContextView`，它挂在情境上，不挂在人上——所以即使这一维存在，也拼不出"某人的好感度曲线"。

被排除的特征：**作者活跃度（standing）**。它是按人聚合的，会让决策依赖"这个人是谁"而不是"这句话是什么场合"。它属于关系模块的领域，不属于决策输入。

### M3 Decision（核心）

| 类 | 说明 | CQ |
|---|---|---|
| `DecisionPoint` | 一次待决：情境 + Agent + 时间 | CQ2 |
| `CandidateAction` | 候选动作抽象类 | CQ3 |
| `Silence` | 沉默 | CQ8 |
| `Reply` | 发言，带 `speechAct` | CQ3 |
| `SpeechAct` | 对话行为，对齐 DiAML 交际功能 | CQ3 |
| `Decision` | 实际选择 + 置信度 + 依据 | CQ2/CQ7 |
| `Confidence` | 样本量、平滑后概率、Wilson 下界 | CQ7 |
| `Outcome` | 发言后 3 条内的走向（continued / resolved / dropped / uncertain） | CQ3 |

`SpeechAct` 的取值来自 `actKind` 词表：answer / agreement / question / tease / complaint / infoShare / media / selfDisclosure / other。只描述**行为类型**，不描述措辞。

### M4 Policy

| 类 | 说明 | CQ |
|---|---|---|
| `PolicyVersion` | 策略版本（含语料哈希、抽取器版本、构建时间） | CQ7/CQ10 |
| `StateBucket` | 特征取值组合 | CQ2 |
| `StateActionStat` | 某状态下某动作的 support / share / 中位延迟 / 延续率 | CQ2 |
| `DecisionTable` | 状态到动作的映射（DMN 语义） | CQ2 |

**关键约束**：`StateBucket` 只能由第 5 节的八个维度构成。任何按 `Participant` 分组的统计都不属于本体的合法实例——这一点写进 SHACL 注释，并在构建器里断言。

### M5 Lexical（群级词汇）

| 类 | 说明 | CQ |
|---|---|---|
| `Term` | 群内词条（`skos:Concept`） | CQ6 |
| `SlangSense` | 词义解释，含定义与出处 | CQ6 |
| `Topic` | 会话级话题（概念集合） | CQ1 |

词义解释的**主语是词，不是人**。"小明解释过这个词"只作为 `Evidence` 的作者出现，不构成"关于小明的知识"。

### M6 Assertion（群级事实）

| 类 | 说明 | CQ |
|---|---|---|
| `Claim` | 关于**概念/考试范围**的断言，如"408 不考红黑树" | CQ6 |
| `Evidence` | 原话片段（字符区间 + 哈希） | CQ4 |

**主语只能是 `Concept` 或 `Topic`，不能是 `Participant`。** 这是 SHACL 里可执行的形状，也是"不做个人断言"的技术保证。

### M7 Provenance

| 类 | 说明 | CQ |
|---|---|---|
| `Evidence` | W3C Web Annotation 风格的片段标注：消息 + 起止 + 引文哈希 | CQ4 |
| `ExtractorRun` | 一次抽取活动（PROV Activity） | CQ10 |
| `OntologyVersion` | 本体版本 | CQ10 |

### M8 Constraint（Agent 自身）

| 类 | 说明 | CQ |
|---|---|---|
| `AgentIdentity` | Kinna 的部署身份 | CQ9 |
| `Capability` | 具备的能力（Skill） | CQ9 |
| `Boundary` | 硬边界：沉默默认、不主动私聊、不编造经历 | CQ9 |
| `RateLimit` | 频率与冷却 | CQ9 |

人格不在这里。人格留在 prompt，本体只存"能不能做"。

---

## 6. 属性与公理

### 6.1 对象属性（节选）

| 属性 | Domain | Range | 逆 | 基数 |
|---|---|---|---|---|
| `hasSituation` | DecisionPoint | Situation | `situationOf` | 1 |
| `hasCandidate` | DecisionPoint | CandidateAction | — | 2..* |
| `chosenAction` | Decision | CandidateAction | — | 1 |
| `basedOn` | Decision | Evidence | `supports` | 1..* |
| `hasConfidence` | Decision | Confidence | — | 1 |
| `hasFeature` | Situation | FeatureValue | — | 1..7 |
| `hasDimension` | FeatureValue | FeatureDimension | — | 1 |
| `hasAuthor` | Message | Participant | `authored` | 1 |
| `addressedTo` | Message | Participant | — | 0..* |
| `hasSpeechAct` | Reply | SpeechAct | — | 1 |
| `silentUnder` | Silence | Situation | — | 1 |
| `inPolicy` | StateActionStat | PolicyVersion | — | 1 |

### 6.2 OWL 公理（能被推理器用上的部分）

```
Silence ⊑ ¬Reply
Claim ⊑ ¬(∃ subject . Participant)          # 断言不以人为主语
Decision ⊑ (=1 chosenAction)
DecisionPoint ⊑ (=1 hasSituation)
FeatureValue ⊑ (=1 hasDimension)
Participant ⊑ (=1 hashedId)                  # 占位节点只有 ID
```

### 6.3 SHACL 形状（闭世界校验，构建时强制）

```turtle
kdo:ParticipantShape a sh:NodeShape ;
  sh:targetClass kdo:Participant ;
  sh:closed true ;
  sh:ignoredProperties ( rdf:type ) ;
  sh:property [ sh:path kdo:hashedId ; sh:maxCount 1 ; sh:minCount 1 ] .

kdo:EvidenceShape a sh:NodeShape ;
  sh:targetClass kdo:Evidence ;
  sh:property [ sh:path kdo:sourceMessage ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path kdo:startOffset ; sh:datatype xsd:integer ; sh:minCount 1 ] ;
  sh:property [ sh:path kdo:endOffset ; sh:datatype xsd:integer ; sh:minCount 1 ] .

kdo:DecisionShape a sh:NodeShape ;
  sh:targetClass kdo:Decision ;
  sh:property [ sh:path kdo:chosenAction ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path kdo:basedOn ; sh:minCount 1 ] ;
  sh:property [ sh:path kdo:hasConfidence ; sh:minCount 1 ; sh:maxCount 1 ] .

kdo:ContextWindowShape a sh:NodeShape ;
  sh:targetClass kdo:ContextWindow ;
  sh:property [ sh:path kdo:message ; sh:node kdo:MessageShape ] .

kdo:SocialContextViewShape a sh:NodeShape ;
  sh:targetClass kdo:SocialContextView ;
  sh:closed true ;
  sh:ignoredProperties ( rdf:type ) ;
  sh:property [ sh:path kdo:sourceModule ; sh:hasValue "agent/memory/relationships" ] ;
  sh:property [ sh:path kdo:viewVersion ; sh:minCount 1 ; sh:maxCount 1 ] ;
  sh:property [ sh:path kdo:observedAt ; sh:datatype xsd:dateTime ; sh:minCount 1 ] ;
  sh:property [ sh:path kdo:affinityBand ;
                sh:in ( "unknown" "low" "neutral" "high" ) ] ;
  sh:property [ sh:path kdo:familiarityBand ;
                sh:in ( "unknown" "low" "medium" "high" ) ] .
```

`ContextWindow` 里"每条消息都早于触发消息"这条时序约束，SHACL 用 SPARQL 约束表达（`sh:sparql`），OWL 表达不了。

`SocialContextViewShape` 是**边界的技术保证**：`sh:closed true` 意味着关系模块的视图只能带这几个字段进来，任何额外属性（比如原始分数、互动明细、性格标签）都会校验失败。想扩字段必须改本体，而不是偷偷塞进来。

### 6.4 不建模的东西（写进本体注释）

- 没有 `Person`、`Profile`、`Preference`、`Emotion`、`Relationship` 类——**它们归 `agent/memory/relationships` 与 `features/mood`，不归本本体**；
- 没有以 `Participant` 为 domain 的聚合属性；
- 没有"最近联系人""常聊对象"这类派生视图；
- `SocialContextView` 只允许分档，不允许分数与明细（SHACL 强制）。

### 6.5 与关系模块的接口契约（外部端口）

```python
# 只读端口，由 relationships 模块实现；KDO 侧只声明接口
def social_context(situation_id: str) -> SocialContextView | None: ...
```

| 条款 | 约定 |
|---|---|
| 调用时机 | 组装 `Situation` 时调用一次，不在决策循环里重复调 |
| 返回 | 有界视图；无数据返回 `None`，决策按 `socialBand=unknown` 继续 |
| 超时 | 50ms；超时即降级为 `unknown`，不阻塞回复 |
| 写入 | **禁止**。KDO 不调用 `apply_proposal`，不碰 `relations` / `affection_events` |
| 批量 | **禁止**按人拉取历史曲线或全量关系表 |
| 版本 | `viewVersion` 随决策落库；关系模块改版不触发 KDO 重算，只影响后续决策 |

这条契约的意义：关系模块可以自由演进它的内部评分，只要分档接口不变，决策本体不受影响；反过来，决策本体换版本也不需要动关系数据。

---

---

## 7. 与存储的关系

本体是规范，不是存储格式。

| 层 | 落地方式 |
|---|---|
| 本体定义 | Turtle/OWL 文件，进版本库 |
| 校验 | SHACL（构建时跑，失败则构建失败） |
| 运行时数据 | SQLite（`nodes` / `edges` 两表 + 决策表），零新增依赖 |
| 离线推理 | rdflib / pyoxigraph（仅构建期，可选） |

不把运行时数据存成 RDF 的理由：运行时只做"桶 → 动作分布"的键值查询，RDF 的图遍历能力用不上，反而增加依赖与延迟。本体负责"什么叫合法"，SQLite 负责"怎么查得快"。

---

## 8. 生命周期

| 层 | 版本对象 | 变更影响 |
|---|---|---|
| 本体 | `OntologyVersion` | 需迁移数据 |
| 抽取器 | `ExtractorRun.extractorVersion` | 需重跑抽取 |
| 策略 | `PolicyVersion` | 只需重建统计 |

废弃用 `status=retired`，不删节点——删了就无法解释历史决策。

---

## 9. 验收：每条 CQ 一个查询用例

| CQ | 查询 | 通过标准 |
|---|---|---|
| CQ1 | 给定消息，取 Situation 及其 8 个 FeatureValue | 8 维齐全（`socialBand` 可为 unknown） |
| CQ2 | 给定 StateBucket，取 StateActionStat | support > 0 且 share 之和 ≈ 1 |
| CQ3 | 给定 Situation，按 actKind 分组统计 | 覆盖 ≥ 80% 的已参与案例 |
| CQ4 | 给定 Decision，回溯 Evidence 到消息字符区间 | 哈希校验通过 |
| CQ5 | 给定消息，取 addressedTo / reply 关系 | 与原文段结构一致 |
| CQ6 | 给定 Term，取 SlangSense 与出处 | 定义非空 |
| CQ7 | 给定 Decision，取 Confidence 与 PolicyVersion | 字段齐全 |
| CQ8 | 给定 Group，统计 Silence : Reply | 与语料实测一致（约 80:20） |
| CQ9 | 给定 DecisionPoint，列出命中的 Boundary / RateLimit | 可枚举 |
| CQ10 | 给定快照，回溯语料哈希与抽取器版本 | 可复现 |
| CQ11 | 给定 Decision，取 `SocialContextView` 的 `viewVersion` 与 `observedAt` | 可追溯；缺失时记为 unknown |

---

## 10. 下一步（待确认后执行）

1. 把第 5、6 节写成 Turtle 本体文件 + SHACL 形状；
2. 给关系模块写一个**只读端口 stub**（返回 `None` 即可跑通），确保 `socialBand=unknown` 路径可用；
3. 用 Protégé 或 rdflib 做一次一致性检查（类不相交、属性域值域不冲突）；
4. 用 11 条 CQ 写 SPARQL 查询，在语料上跑通；
5. 通过后再谈构建管线与决策表落地。

在 1～4 跑通之前，不写业务代码。关系模块现有代码**不动**——它已经是对的，只是不归本体重写。

---

## 参考

- Noy & McGuinness, *Ontology Development 101*（七步法与能力问题）
- SIOC 规范：http://rdfs.org/sioc/spec/
- PROV-O：https://www.w3.org/TR/prov-o/
- SKOS：https://www.w3.org/TR/skos-reference/
- SHACL：https://www.w3.org/TR/shacl/
- OWL-Time：https://www.w3.org/TR/owl-time/
- ISO 24617-2（DiAML）：https://www.iso.org/standard/76443.html
- OMG DMN：https://www.omg.org/dmn/
- CoALA（记忆分层参考，非直接采用）：https://arxiv.org/abs/2309.02427
