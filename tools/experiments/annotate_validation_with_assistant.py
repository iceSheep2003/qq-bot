"""Attach an explicit current-model draft label to held-out packets.

The mapping is deliberately visible and reviewable; it is not hidden training
data and it is never used to pretend that a human gold label exists.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


LABELS = {
    "v01": ("agree", "agree", "none", "接住当前共识，不重复解释", 0.78),
    "v02": ("banter", "joke", "none", "把明显的夸张说法当玩笑接住，不执行字面动作", 0.86),
    "v03": ("answer_directly", "answer", "none", "这是关于是否学雅思的直接问题，先给简短判断", 0.71),
    "v04": ("agree", "agree", "none", "对上一条判断做简短确认", 0.82),
    "v05": ("supplement", "add_information", "none", "补充自己的资料计划，不抢走话题", 0.73),
    "v06": ("clarify", "clarify", "none", "对做法是否可靠保持不确定，先说明条件", 0.66),
    "v07": ("agree", "agree", "none", "对学校难度判断做轻量附和", 0.76),
    "v08": ("banter", "joke", "none", "顺着吓哭的夸张梗接一句，不展开科普", 0.88),
    "v09": ("answer_directly", "answer", "none", "回答赛事结果或数据问题，无法确认时不要编造", 0.63),
    "v10": ("supplement", "add_information", "none", "在资料对话中补充自己的经验", 0.79),
    "v11": ("tease_gently", "tease", "none", "轻度接住调侃，不扩大成质问", 0.76),
    "v12": ("answer_directly", "answer", "none", "直接解释三个方法的用途区别", 0.84),
    "v13": ("clarify", "clarify", "none", "对数学推导结论给出边界说明", 0.82),
    "v14": ("answer_directly", "answer", "none", "回答考研年份问题，必要时保持人格口吻", 0.78),
    "v15": ("acknowledge", "acknowledge", "none", "先确认对方说法，不额外扩展封禁判断", 0.61),
    "v16": ("clarify", "clarify", "none", "当前只有一个词，先确认它指什么，不强行回答", 0.84),
    "v17": ("banter", "joke", "none", "明显是骗你的玩笑，轻松接梗即可", 0.93),
    "v18": ("answer_directly", "answer", "none", "回答题型是否固定，说明不确定范围", 0.79),
    "v19": ("acknowledge", "acknowledge", "none", "极短感叹不适合展开，轻量回应或等待", 0.62),
    "v20": ("acknowledge", "acknowledge", "none", "承接赛事信息，不擅自转成专业分析", 0.69),
    "v21": ("answer_directly", "answer", "none", "回答教材勘误内容，保持简短", 0.77),
    "v22": ("answer_directly", "answer", "none", "对分数预期给有条件的回答，不作保证", 0.82),
    "v23": ("answer_directly", "answer", "none", "直接回答算术判断", 0.90),
    "v24": ("acknowledge", "acknowledge", "none", "承接资料评价，不需要长篇解释", 0.73),
    "v25": ("agree", "agree", "none", "对正确性做简短确认", 0.91),
    "v26": ("supplement", "add_information", "none", "补充学习方法经验，同时接住挫败感", 0.74),
    "v27": ("clarify", "clarify", "none", "补充对称条件这一关键边界", 0.86),
    "v28": ("banter", "joke", "none", "把夸张分数当群内玩笑，不认真比较", 0.84),
    "v29": ("clarify", "clarify", "none", "术语不清，先确认缩写含义", 0.89),
    "v30": ("answer_directly", "answer", "none", "直接说明自己没有听到相关内容", 0.80),
    "v31": ("acknowledge", "acknowledge", "none", "收到式短回应，不新增问题", 0.94),
    "v32": ("answer_directly", "answer", "none", "回答资料来源平台问题", 0.84),
    "v33": ("answer_directly", "answer", "none", "否定上一种理解并等待继续说明", 0.74),
    "v34": ("encourage", "encourage", "none", "用短句鼓励目标分数，不承诺结果", 0.87),
    "v35": ("clarify", "clarify", "none", "解释连续条件与两个动点的关系", 0.88),
    "v36": ("answer_directly", "answer", "none", "回答服务封号风险，不能凭空保证", 0.81),
    "v37": ("clarify", "clarify", "none", "当前问题指代不明，先确认发生了什么", 0.87),
    "v38": ("answer_directly", "answer", "none", "对难度比较给条件化判断", 0.78),
    "v39": ("empathize", "empathize", "none", "先接住总结不出规律的挫败感，不连续追问", 0.88),
    "v40": ("acknowledge", "acknowledge", "none", "承接分数信息，避免无依据地夸张评价", 0.72),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.packets.open(encoding="utf-8") if line.strip()]
    missing = {row["sample_id"] for row in rows} - LABELS.keys()
    if missing:
        raise SystemExit(f"missing visible draft labels: {sorted(missing)}")
    output = []
    for row in rows:
        strategy, action, media, rationale, confidence = LABELS[row["sample_id"]]
        output.append({
            **row,
            "assistant_draft": {
                "strategy": strategy,
                "dialogue_act": action,
                "media_intent": media,
                "avoid": ["不要机械追问", "不要把不确定内容说成事实"],
                "rationale": rationale,
                "confidence": confidence,
                "annotator": "current-model-explicit-draft-v1",
            },
            "review": {"status": "pending_deepseek_review"},
        })
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = args.output.with_suffix(args.output.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as stream:
        os.chmod(tmp, 0o600)
        for row in output:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(args.output)
    print(json.dumps({"cases": len(output), "status": "assistant_draft_pending_deepseek_review"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
