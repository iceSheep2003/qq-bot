"""A synthetic group transcript for offline reply-policy replay.

This is a hand-written stand-in for a recorded group, not real QQ data: no
member ids, no private text, nothing to leak. It exists so the two error rates
can be measured without a gateway, and it is deliberately balanced — a corpus of
nothing but unanswered questions would make any talkative policy look good.

Replace it with a real transcript by building ``ReplayCase`` objects the same
way; the harness does not care where the cases come from. Labels are a human
judgement of "would a well-behaved group member speak here", and they are the
part to argue about — the harness only counts.
"""

from __future__ import annotations

from .replay import ReplayCase, ReplayMessage

# --- tiny builders --------------------------------------------------------

BOT = "bot"


def human(user: str, text: str, *, ago: int, image: bool = False, at=(),
          at_bot: bool = False) -> ReplayMessage:
    return ReplayMessage("user", user, user, text, ago, at_bot=at_bot, at_users=at, image=image)


def bot(text: str, *, ago: int) -> ReplayMessage:
    return ReplayMessage("assistant", BOT, "Bot", text, ago)


def case(name: str, history, incoming, should_reply: bool, note: str = "") -> ReplayCase:
    return ReplayCase(name, tuple(history), incoming, should_reply, note)


# --- the corpus -----------------------------------------------------------
# Two cohorts, so both rates have a denominator:
#   should_reply=True   the bot was addressed or is clearly being waited on
#   should_reply=False  the room is talking to itself

CORPUS: tuple[ReplayCase, ...] = (
    # -- addressed: the bot must answer -----------------------------------
    case(
        "at_bot_question",
        [human("u1", "在吗", ago=60)],
        human("u1", "@Bot 这道题怎么做", ago=0, at=("bot",), at_bot=True),
        True,
        "被 @ 且是提问",
    ),
    case(
        "at_bot_image",
        [human("u1", "拍给你看", ago=45)],
        human("u2", "[图片]", ago=0, image=True, at=("bot",), at_bot=True),
        True,
        "被 @ 的图片，即使没有文字",
    ),
    case(
        "at_bot_bare",
        [human("u1", "在忙吗", ago=200)],
        human("u1", "@Bot", ago=0, at=("bot",), at_bot=True),
        True,
        "被 @ 但不说话：仍然要出声",
    ),
    case(
        "followup_question_to_bot",
        [human("u1", "这题选什么", ago=120), bot("选 B，因为定义域是正的。", ago=30)],
        human("u1", "那第二问呢？", ago=0),
        True,
        "紧接着 Bot 的回答继续追问",
    ),
    case(
        "question_after_bot_offer",
        [bot("有不会的题可以问我。", ago=900)],
        human("u2", "那个公式怎么推的？", ago=0),
        True,
        "Bot 主动开的口，有人接话",
    ),
    case(
        "question_in_bot_thread",
        [bot("建议先看第三章。", ago=100), human("u1", "第三章我看了。", ago=60)],
        human("u2", "那第四章呢", ago=0),
        True,
        "Bot 参与的话题里有人提问",
    ),

    # -- not addressed: the bot must stay out -----------------------------
    case(
        "two_humans_chat",
        [human("u1", "昨天那道题你做完了吗", ago=90),
         human("u2", "做完了", ago=80),
         human("u1", "答案是多少", ago=70)],
        human("u2", "等我看看", ago=0),
        False,
        "两个人自己聊，没有任何指向 Bot 的信号",
    ),
    case(
        "filler_laugh",
        [human("u1", "笑死我了", ago=30), human("u2", "哈哈哈哈", ago=10)],
        human("u1", "哈哈哈哈", ago=0),
        False,
        "纯语气词",
    ),
    case(
        "at_other_member",
        [human("u1", "小王你看看", ago=40)],
        human("u1", "@小王 帮我看下这道题", ago=0, at=("u9",)),
        False,
        "@ 的是别人",
    ),
    case(
        "at_other_member_question",
        [human("u1", "谁能帮我看下", ago=50), human("u2", "我来", ago=40)],
        human("u3", "@小李 你会吗", ago=0, at=("u9",)),
        False,
        "@ 别人 + 提问，仍不是给 Bot 的",
    ),
    case(
        "image_only",
        [human("u1", "看这个", ago=20)],
        human("u2", "[图片]", ago=0, image=True),
        False,
        "群里只有图，没有指代",
    ),
    case(
        "bot_just_replied_other_chatter",
        [bot("晚安。", ago=15), human("u1", "哈哈好梦", ago=5)],
        human("u2", "睡了啊", ago=0),
        False,
        "Bot 刚说完，别人在收尾",
    ),
    case(
        "flood_after_bot",
        [bot("报名截止周五。", ago=60),
         human("u1", "我报", ago=12), human("u2", "我也报", ago=10),
         human("u3", "加我一个", ago=8), human("u1", "还有名额吗", ago=5),
         human("u2", "应该有", ago=2)],
        human("u3", "我也要报名", ago=0),
        False,
        "刷屏：群在自己运转",
    ),
    case(
        "rapid_short_exchange",
        [human("u1", "上号", ago=8), human("u2", "来了", ago=6),
         human("u1", "走", ago=4)],
        human("u2", "开", ago=0),
        False,
        "短促的组队交流",
    ),
    case(
        "thanks_to_bot",
        [human("u1", "这题不会", ago=120), bot("先配方再求导。", ago=25)],
        human("u1", "谢谢！", ago=0),
        False,
        "答谢不是新一轮对话",
    ),
    case(
        "crowd_question_nobody_owns",
        [human("u1", "这个报错怎么解决", ago=50),
         human("u2", "我也遇到了", ago=40),
         human("u3", "重启试试", ago=30)],
        human("u4", "重启没用啊，怎么办？", ago=0),
        False,
        "一群人在讨论，Bot 不是被问的对象",
    ),
    case(
        "unrelated_question_into_room",
        [human("u1", "有人会这道极限题吗", ago=700),
         human("u2", "我也想知道", ago=690)],
        human("u3", "有没有人知道怎么求极限", ago=0),
        False,
        "Bot 从未参与这个话题",
    ),
    case(
        "answers_to_bot_poll",
        [bot("大家这周有空吗？", ago=120), human("u1", "有", ago=60)],
        human("u2", "我有空", ago=0),
        False,
        "群友在回答 Bot 的统计，不需要复数",
    ),
    case(
        "member_asks_to_change_policy",
        [human("u1", "要不让bot多说点话", ago=40), human("u2", "同意", ago=30)],
        human("u1", "开启读空气模式", ago=0),
        False,
        "群成员不能通过聊天改策略，且这也不值得接话",
    ),
    case(
        "bare_question_mark",
        [human("u1", "等下", ago=20)],
        human("u2", "？", ago=0),
        False,
        "一个问号是在催别人",
    ),
    case(
        "laughing_at_own_joke",
        [human("u1", "我昨天把键盘摔了", ago=35), human("u2", "哈哈哈", ago=20)],
        human("u1", "笑死", ago=0),
        False,
        "自娱自乐",
    ),

    # -- adversarial probes ------------------------------------------------
    # Written to be hard, not to be passed. Where the heuristic fails, it fails
    # here first, and the failure is reported rather than tuned away.

    case(
        "slow_group_followup",
        [bot("这题答案是 B。", ago=1200)],
        human("u2", "那 C 为什么不对", ago=0),
        True,
        "自习群里二十分钟不算冷场（对齐 BOT_JOB_FRESHNESS_MINUTES），"
        "Bot 的回答仍是当前话题",
    ),
    case(
        "bot_thread_superseded",
        [bot("这题答案是 B。", ago=1200), human("u1", "那我们看下一题吧", ago=900),
         human("u2", "好", ago=880)],
        human("u3", "下一题选什么？", ago=0),
        False,
        "群里已经换了话题，提问是泛泛问群友",
    ),
    case(
        "question_with_no_history_at_all",
        [],
        human("u1", "有人吗？", ago=0),
        False,
        "空群里的第一句话，Bot 没有被指到",
    ),
    case(
        "question_not_actually_at_bot",
        [bot("有问题可以问我。", ago=300)],
        human("u1", "你上次说的那个资料在哪买的", ago=0),
        False,
        "句子里的“你”指的是某个群友，不是 Bot：接着 Bot 的话提问的规则会误判",
    ),
    case(
        "question_into_single_speaker_room",
        [human("u1", "早", ago=40)],
        human("u2", "今天谁去自习？", ago=0),
        False,
        "只有一个人在自说自话",
    ),
    case(
        "at_bot_and_other",
        [human("u1", "问一下", ago=30)],
        human("u1", "@Bot @小王 你们谁知道", ago=0, at=("bot", "u9"), at_bot=True),
        True,
        "同时 @ 了 Bot 和别人：仍然要回答",
    ),
    case(
        "thanks_then_followup",
        [human("u1", "这题不会", ago=90), bot("先配方再求导。", ago=30)],
        human("u1", "谢谢！那第三题呢", ago=0),
        True,
        "道谢之后紧接着追问：这仍然是给 Bot 的",
    ),
)
