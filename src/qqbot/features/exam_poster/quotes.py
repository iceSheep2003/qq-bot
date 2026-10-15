"""Original short encouragements for the daily card (no model call)."""

from __future__ import annotations

from datetime import date

# 19 is coprime to the six bundled campuses, so a school does not always get
# the same three lines. These are original copy, not unattributed quotations.
MOTTOS = (
    "今天先把眼前这一页走完，远方会慢慢变近。",
    "慢一点也在向前，别把自己落下。",
    "你认真坐下来的每一天，都在替未来铺路。",
    "别急着证明自己，先把今天过扎实。",
    "走到这里已经很不容易，继续往前就好。",
    "不是每天都要闪光，稳稳走过也很厉害。",
    "翻过去的不只是书页，还有昨天的犹豫。",
    "今天的安静努力，会在某一天有回声。",
    "先照顾好自己，再去奔赴想去的地方。",
    "不用和别人比快，把自己的路走实就好。",
    "看不见结果的时候，也别低估这一小步。",
    "你在积累的，不止是答案，还有底气。",
    "允许今天难一点，明天再往前一点。",
    "把焦虑放旁边，先做手边这一件事。",
    "愿你赶路时，也记得抬头看看天。",
    "再多一点耐心，给认真一个开花的机会。",
    "有些日子只是平凡地坚持，也值得记住。",
    "一步一步来，山顶不会因为你慢就消失。",
    "你已经在路上了，今天也算数。",
)


def motto_for(today: date) -> str:
    return MOTTOS[today.toordinal() % len(MOTTOS)]
