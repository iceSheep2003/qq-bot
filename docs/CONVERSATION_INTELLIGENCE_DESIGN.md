# 群聊理解与上下文选择设计

状态：Phase A–D 的首个可运行版本已接入。话题观察可在重启后重建，Prompt Session 采用 append-only 并在阈值处显式压缩，情绪与关系观察可随主回复一次返回；远程 embedding 仍保持可选且默认不启用。

目标是在不增加一次大模型调用的前提下，让 Bot 在多人、多话题并行的 QQ 群中确定“谁在和谁说话、当前在谈什么、回复时应读取哪些消息”，再把相关记忆、情绪、黑话和人物关系组织成一次主模型调用的输入。

本文同时约束 Prompt Cache：已经发给模型的消息不可在下一轮重新渲染；旧请求必须尽量成为新请求的逐字节前缀。

## 1. 边界与依赖方向

新增有界上下文 `conversation_intelligence`，它不发送 QQ 消息、不调用主模型、不修改记忆、情绪、关系或人格数据。

```text
domain / ports
       ↑
conversation_intelligence
       ↑
storage adapters / optional embedding adapter
       ↑
runtime service / app assembly
```

建议目录：

```text
qunbot/conversation_intelligence/
├── models.py          # MessageView、TopicState、ConversationFrame
├── normalizer.py      # 纯文本归一化、token、实体和语气信号
├── graph.py           # 引用、@、相邻消息和说话人关系边
├── threader.py        # 在线话题归属，不调用模型
├── selector.py        # 按相关性和预算选择消息
├── assembler.py       # 组合记忆、情绪、黑话、人物关系
├── service.py         # AnalyzeConversation 用例
└── ports.py           # 仓储、可选 embedding、各领域只读视图

qunbot/storage/
├── conversation_topics.py
└── prompt_sessions.py
```

`runtime.Agent` 最终只消费 `ConversationFrame` 和 `PromptSession`，不能反向知道 SQLite、NapCat、黑话实现或具体 embedding 服务。

## 2. 入站消息契约

当前 `MessageEvent` 缺少引用消息信息。需要以兼容方式增加：

```python
@dataclass(frozen=True)
class MessageEvent:
    # 已有字段省略
    platform_message_id: str = ""
    reply_to_message_id: str = ""
    reply_to_user_id: str = ""
    message_segments: tuple[MessageSegment, ...] = ()
```

NapCat/OneBot 的 `reply` segment 提供被回复消息 ID。若事件只给出 ID，不为了分析同步请求网络；先查本地 RawEventLog，查不到时把它保留为未知引用。事件接收不得因为引用解析失败而阻塞。

原始数据与派生数据分开：

- `RawEventLog`：平台事实，是话题重算与审计的依据。
- `TopicStore`：可丢弃、可重建的派生索引。
- `PromptSessionLog`：模型真正看到过的精确消息，用于缓存连续性。
- `messages`：现有用户可读会话和记忆采集来源。

## 3. 本地文本特征

默认算法不增加第三方分词器和网络请求。

每条文本生成 `MessageFeatures`：

```python
@dataclass(frozen=True)
class MessageFeatures:
    normalized: str
    cjk_bigrams: frozenset[str]
    ascii_tokens: frozenset[str]
    entities: frozenset[str]
    mentioned_users: frozenset[str]
    punctuation: frozenset[str]
    tone: ToneSignal
```

处理规则：

1. Unicode NFKC、casefold、折叠空白。
2. 连续中文生成二元字符组；最多保留 80 个，忽略常见停用片段。
3. 英文、数字、课程代号和缩写按 `[0-9a-z_+#.-]+` 提取。
4. `#话题`、URL host、课程名、数字年份、群内已审核黑话作为实体。
5. `[图片]`、表情和纯 @ 不产生伪关键词，但保留消息类型信号。

本地语气只产生弱信号，不直接修改 Bot 情绪：

```python
ToneSignal(
    polarity=-1.0..1.0,
    intensity=0.0..1.0,
    question=True | False,
    joking=True | False,
    hostile=True | False,
)
```

它由审核过的情绪词表、否定词、重复标点、emoji 和文本长度计算。主模型仍结合语境判断最终表达；本地信号只帮助选上下文和提示风险。

## 4. 消息图

群聊不是单链表，而是有向消息图。每条新消息添加以下边：

| 边 | 权重 | 说明 |
| --- | ---: | --- |
| `reply_to` | 1.00 | 明确引用，最高可信度 |
| `mentions_author` | 0.88 | @ 某条近期消息的作者 |
| `same_entity` | 0.55 | 共享明确实体或已审核黑话 |
| `lexical_similarity` | 0.00–0.50 | 本地 token 相似度 |
| `same_participant` | 0.18 | 同一批参与者的弱证据 |
| `adjacent` | 0.05–0.20 | 时间接近但不能单独决定话题 |

禁止仅因两条消息相邻就认定同一话题。群聊高峰中，相邻消息经常属于不同对话。

## 5. 在线话题划分

每群仅维护最近的有限活动话题：

```python
@dataclass(frozen=True)
class TopicState:
    topic_id: str
    anchor_message_id: str
    message_ids: tuple[str, ...]
    participant_ids: frozenset[str]
    lexical_centroid: Mapping[str, float]
    entity_counts: Mapping[str, int]
    last_active_at: int
    title_hint: str
```

默认上限：

- 每群最多 12 个活动话题。
- 话题最多保留 40 个消息引用。
- 30 分钟无活动转为 dormant。
- 24 小时后从在线索引移除，但 RawEventLog 不删除。

新消息对候选话题计算：

```text
score =
    1.00 * explicit_reply
  + 0.70 * mentioned_participant
  + 0.55 * entity_jaccard
  + 0.45 * lexical_jaccard
  + 0.20 * participant_overlap
  + 0.15 * time_decay
  + 0.10 * adjacency
```

规则优先于分数：

1. 本地能找到被引用消息时，直接继承其 topic。
2. 同时命中多个话题时，明确引用优先；否则分数最高者胜出。
3. 最高分低于 `0.46` 时创建新话题。
4. 第一名和第二名差值小于 `0.08` 时标记 `ambiguous`，不强行合并；Selector 同时提供两个小窗口给主模型。
5. 话题不得因为单个通用词（“这个”“然后”“哈哈”）合并。

词法相似度采用加权 Jaccard；稀有 token 权重大于高频 token。群内 token 文档频率由有界计数器维护，无需模型。

可选 `EmbeddingPort` 只在以下情况参与：没有引用关系、词法前两名接近且本地已有 embedding。它不得同步调用远程 embedding 阻塞回复；缓存未命中时直接按词法结果继续。

```python
class EmbeddingView(Protocol):
    def cached_vector(self, message_id: str) -> tuple[float, ...] | None: ...
```

## 6. 回复目标判定

`TargetResolver` 输出事实和置信度，不决定回复内容：

```python
@dataclass(frozen=True)
class ReplyTarget:
    focus_message_id: str
    target_user_id: str
    quoted_message_id: str
    topic_id: str
    confidence: float
    reasons: tuple[str, ...]
```

优先顺序：

1. 当前消息明确引用。
2. 当前消息 @ Bot，同时引用或点名某位群友。
3. 当前消息 @ Bot，目标为当前发言人及其话题。
4. 主动接话时，目标为活跃话题而非任意最后一句。

只有平台提供的 QQ ID 才能成为发送层的 @ 目标；文本中看起来像 QQ 号的字符串不授予权限。

## 7. 消息选择器

选择器使用“硬包含 + 相关性排序 + 分区预算”，不使用固定最近 N 条。

### 7.1 硬包含

以下消息只要存在就必须进入：

- 当前触发消息。
- 引用链，向上最多 4 层。
- Bot 在当前话题中的最后一条回复。
- 引用链涉及的消息作者最近一条必要澄清。

### 7.2 话题窗口

从当前 Topic 中按下式排序：

```text
relevance =
    1.00 * graph_proximity
  + 0.65 * lexical_similarity_to_focus
  + 0.45 * entity_overlap
  + 0.25 * participant_relevance
  + 0.20 * recency
```

默认最多 12 条、2400 字符。保留时间顺序后交给模型，不能按分数顺序展示，否则会破坏对话含义。

### 7.3 环境窗口

额外选择最多 4 条、500 字符的附近群消息，用于判断群氛围和是否存在其他话题。环境消息明确标记为 `ambient`，不能伪装成当前话题。

### 7.4 预算降级

预算不足时依次删除：

1. 低相关 ambient。
2. 当前话题中的重复附和和纯表情。
3. 最旧的非引用链消息。

永不删除当前消息和明确引用链。图片消息只保留类型、来源消息 ID 和最多两张经过验证的当前图片 URL。

## 8. 领域上下文装配

`FrameAssembler` 只通过只读端口读取其他模块：

```python
class MemoryContextView(Protocol):
    def related(self, scope: str, query: str, limit: int) -> list[str]: ...

class RelationshipContextView(Protocol):
    def summaries(self, group_id: str, user_ids: tuple[str, ...]) -> dict[str, str]: ...

class EmotionContextView(Protocol):
    def narration(self, scope: str) -> str: ...

class SlangContextView(Protocol):
    def explain_present(self, group_id: str, texts: tuple[str, ...]) -> dict[str, str]: ...
```

记忆检索 query 不是当前一句话，而是：

```text
当前消息 + topic关键词 + 明确实体 + reply target + 最近问题句
```

黑话模块只解释选中消息中实际出现的已审核词条。人物关系只读取 focus user、引用对象和最多两个重要参与者。情绪读取 Bot 当前状态，并叠加本地计算的 `room_tone`；二者不能相互写数据。

最终值对象：

```python
@dataclass(frozen=True)
class ConversationFrame:
    frame_id: str
    scope: str
    focus: MessageView
    target: ReplyTarget
    topic: TopicView
    selected_message_ids: tuple[str, ...]
    ambient_message_ids: tuple[str, ...]
    participants: tuple[ParticipantView, ...]
    memories: tuple[str, ...]
    slang: Mapping[str, str]
    bot_mood: str
    room_tone: ToneSignal
    persona_strategy: str
    constraints: tuple[str, ...]
```

所有集合在渲染前使用明确稳定顺序；禁止依赖 `set` 或数据库未指定的行顺序。

## 9. Prompt Cache 与 Session Log

展示会话不能再被用来重建模型历史。每群拥有独立 `PromptSessionLog`，保存精确 OpenAI message JSON：

```sql
CREATE TABLE prompt_session_events (
  id INTEGER PRIMARY KEY,
  scope TEXT NOT NULL,
  series_id TEXT NOT NULL,
  sequence INTEGER NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  source_event_id TEXT,
  created_at INTEGER NOT NULL,
  UNIQUE(scope, series_id, sequence),
  UNIQUE(scope, series_id, kind, source_event_id)
);
```

单轮流程：

1. RawEventLog 已经记录群消息。
2. Conversation Intelligence 选择本轮需要但尚未进入 PromptSession 的消息。
3. 把选中的原始消息以稳定 JSON 批次追加到 session。
4. 追加只引用 message ID 的 `ConversationFrame`；避免再次复制全部正文。
5. 调用主模型。
6. 原样追加模型返回的 assistant message，包括 tool calls。
7. 原样追加每个 tool result。
8. 用户可见的解析后回复另存到 `messages`。

下一轮只能在尾部追加，不得重新格式化第 3～7 步。旧动态快照即使过时也留在历史中，新快照声明覆盖旧版本。

示意：

```text
[stable system]
[observations batch #1]
[frame #1]
[assistant raw #1]
[observations batch #2]
[frame #2]
[assistant raw #2]
```

这样上一轮请求和输出能够成为下一轮请求前缀。DeepSeek 类 KV Cache 可以复用整个旧 Session，而不仅是短 system。

### 压缩

不再每轮滑动删除一两条旧消息。达到模型上下文的 60% 时，在清晰边界开启新 `series_id`：

```text
stable system
latest topic summaries
active participant facts
unresolved questions
recent exact tail
```

摘要优先使用主回复 JSON 中顺带产生的 `topic_update`，不单独调用模型。只有没有可用摘要且必须压缩时，才允许低频批量压缩调用；这是可配置后台任务，不在回复关键路径。

## 10. 主模型协议

主回复协议增加可选派生状态，但仍然只有一次大模型请求：

```json
{
  "text": "...",
  "channels": ["text"],
  "meme_tag": "",
  "voice_text": "",
  "at_user_id": "",
  "intent": "reply",
  "topic_update": {
    "topic_id": "t-123",
    "summary": "408复习顺序讨论",
    "unresolved": ["组成原理应该何时开始"]
  },
  "state_observations": {
    "relationship_signal": "friendly",
    "mood_signal": "encouraged",
    "memory_candidates": []
  }
}
```

这些字段只是提议：关系、情绪和记忆模块分别验证、限幅、幂等写入。模型不能直接更改分数或数据库。解析失败只丢弃状态提议，不影响用户可见回复。

## 11. 失败策略

- TopicStore 故障：退化为引用链 + 最近 8 条，不阻止回复。
- 引用目标缺失：保留 unknown reference，不联网追查。
- 本地特征异常：仅使用时间和参与者信号。
- embedding 不可用：完全忽略，不重试、不等待。
- 黑话、记忆、情绪任一读取失败：该字段为空，其余 Frame 正常生成。
- PromptSession 写入失败：不得发送一个无法重放的模型请求；回退到当前旧路径并记录一次 cache continuity break。
- 模型回复成功但 QQ 发送失败：原始 assistant 仍进入 Session，并追加 delivery failure 事件，避免模型下一轮误以为已经成功送达。

## 12. 分阶段实现

### Phase A：纯本地群聊理解

1. 扩展 `MessageEvent` 与 OneBot reply segment 解析。
2. 新增 models、normalizer、graph、threader、selector。
3. 使用内存 TopicStore 编写离线回放测试。
4. 尚不改 Agent 请求，先记录分析结果用于评估。

验收：同一段录制群聊重复分析得到完全相同 topic ID、消息选择和顺序；无网络和模型调用。

### Phase B：领域上下文装配

1. 接入只读关系、记忆、情绪、黑话端口。
2. Agent 从 `ConversationFrame` 获取动态上下文。
3. 对“多话题并行、引用旧消息、只发一句‘这个呢’、群内黑话”建立回放用例。

验收：不会串群、不会把 ambient 当主话题、记忆 query 包含话题实体。

### Phase C：缓存连续 Session

1. 新增 PromptSessionStore 和 schema migration。
2. 原样持久化 user/assistant/tool model messages。
3. 替换 Agent 的 `recent(18)` 重建路径。
4. cache metrics 增加 `purpose`、`scope_hash`、`tools_hash`、`append_only`。

验收：第二轮完整请求严格以第一轮请求和原始 assistant 输出开头；测试按 canonical bytes 比较，不只比较文本含义。

### Phase D：合并状态观察与压缩

1. 回复 JSON 增加状态提议和 topic update。
2. 情绪、好感度不再默认各调用一次大模型。
3. 增加显式 series compaction。

验收：普通回复只有一次主模型请求；每个状态模块仍有自己的校验、冷却和幂等规则。

## 13. 测试矩阵

- A、B 同时讨论两个话题，消息交叉到达。
- 当前消息引用 30 条以前的消息。
- “这个呢”“然后呢”等无实体短句通过引用链接回正确话题。
- 两个话题恰好有相同参与者但实体不同。
- 同一黑话在不同群有不同审核解释。
- 纯表情、图片、撤回引用目标、未知引用。
- 消息重投、乱序、相同时间戳。
- TopicStore 重启恢复及派生索引重建。
- Topic 分数临界与 ambiguous 双窗口。
- 1000 条群消息下的有界内存和单条分析延迟。
- 下一轮 PromptSession 是上一轮的严格 append-only 扩展。

性能目标：普通文本消息本地分析 P95 小于 10ms；单群活动 Topic 内存小于 256KB；回复关键路径不新增网络请求和大模型调用。
