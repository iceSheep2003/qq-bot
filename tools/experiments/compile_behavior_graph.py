"""Compile extracted behavior proposals into a versioned logical graph.

The compiler never creates person nodes.  It promotes only repeated behavior
patterns; single examples remain candidate evidence until later evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from pathlib import Path


PIPELINE_VERSION = "behavior-graph-compile-v4"

SITUATION_FAMILIES = (
    ("direct_question", "明确提问或求助", ("询问", "求助", "怎么", "哪位", "推荐", "资料")),
    ("banter_or_joke", "玩笑、调侃或群内梗", ("调侃", "玩笑", "夸张", "幽默", "梗", "反问")),
    ("emotional_complaint", "抱怨、疲惫或负面情绪", ("抱怨", "焦虑", "崩", "懊恼", "失望", "难受", "累")),
    ("agreement_or_consensus", "附和、认同或形成共识", ("同意", "确实", "共识", "认同", "肯定", "观点")),
    ("clarification_or_dispute", "概念澄清或意见分歧", ("争议", "分歧", "误解", "区别", "定义", "澄清", "质疑")),
    ("progress_or_good_news", "进度分享或好消息", ("分享", "进步", "成绩", "成功", "好消息", "提升")),
    ("topic_shift_or_cold_room", "话题转移或群聊冷场", ("转移", "冷场", "换话题", "接不上", "自然转移")),
    ("privacy_or_uncertainty", "隐私边界或信息不确定", ("不知道", "不确定", "隐私", "不愿透露", "未确认")),
)

EXPRESSION_FAMILIES = {
    "brief_ack": "简短附和或确认",
    "agree_then_add": "先认同再补充",
    "direct_answer": "直接回答核心问题",
    "answer_with_reason": "回答并给出简短理由",
    "empathy_then_support": "先接住情绪再支持",
    "banter_exaggeration": "夸张或反差接梗",
    "banter_question": "用轻松反问接梗",
    "gentle_tease": "克制的轻度调侃",
    "clarify_definition": "用定义或边界澄清",
    "clarify_example": "用类比或例子澄清",
    "supplement_context": "补充背景或经验",
    "deescalate_uncertainty": "用不确定语气缓和",
    "topic_bridge": "承接后自然转话题",
    "celebrate_brief": "短句庆祝或祝贺",
    "abstain_uncertain": "明确表示不知道或暂不参与",
    "general_expression": "一般口语表达",
}


def sid(kind: str, value: str) -> str:
    digest = hashlib.sha256((kind + "\0" + value).encode()).hexdigest()[:24]
    return f"{kind}_{digest}"


def clean(value: str) -> str:
    return re.sub(r"\s+", " ", str(value)).strip().lower()


def situation_family(text: str, cues: list[str]) -> tuple[str, str]:
    """Map free-form model prose to a small retrieval taxonomy.

    The original sentence remains on InteractionCase.  This mapping is only a
    deterministic index key, never a claim that a keyword proves intent.

    The family is derived from the *situation text and cues alone*.  It used to
    fall back to the case's own strategy when no keyword matched, which made the
    mapping partly circular: 15% of cases were filed under a family *because* of
    the very label that family was later used to predict.  Every statistic read
    off the family — "does the situation predict the strategy?" — was therefore
    self-fulfilling for those cases, and the measured ceiling was inflated by
    1.7pt (13 classes) / 3.0pt (9 classes).

    Taking the strategy out of the signature is deliberate: while the parameter
    exists, the shortcut can come back.  Cases no marker matches land in
    ``general_group_exchange``, which is a real answer rather than a failure —
    the markers cover the situations this taxonomy names, and everything else
    genuinely is ordinary group chatter.
    """
    haystack = clean(" ".join([text, *cues]))
    for key, label, markers in SITUATION_FAMILIES:
        if any(marker in haystack for marker in markers):
            return key, label
    return "general_group_exchange", "一般群聊互动"


def expression_family(text: str, strategy: str, action: str) -> tuple[str, str]:
    """Reduce free-form wording advice to reusable expression structures."""
    value = clean(text)
    if strategy == "abstain" or action == "wait":
        return "abstain_uncertain", EXPRESSION_FAMILIES["abstain_uncertain"]
    if strategy in {"banter", "tease_gently"}:
        if "反问" in value or "疑问" in value:
            return "banter_question", EXPRESSION_FAMILIES["banter_question"]
        if any(mark in value for mark in ("夸张", "反差", "幽默", "梗")):
            return "banter_exaggeration", EXPRESSION_FAMILIES["banter_exaggeration"]
        return "gentle_tease", EXPRESSION_FAMILIES["gentle_tease"]
    if strategy in {"acknowledge", "agree"}:
        if any(mark in value for mark in ("补充", "再", "延伸")):
            return "agree_then_add", EXPRESSION_FAMILIES["agree_then_add"]
        return "brief_ack", EXPRESSION_FAMILIES["brief_ack"]
    if strategy == "answer_directly":
        if any(mark in value for mark in ("解释", "理由", "说明")):
            return "answer_with_reason", EXPRESSION_FAMILIES["answer_with_reason"]
        return "direct_answer", EXPRESSION_FAMILIES["direct_answer"]
    if strategy in {"empathize", "encourage"}:
        return "empathy_then_support", EXPRESSION_FAMILIES["empathy_then_support"]
    if strategy == "clarify":
        if any(mark in value for mark in ("类比", "例子", "比喻")):
            return "clarify_example", EXPRESSION_FAMILIES["clarify_example"]
        return "clarify_definition", EXPRESSION_FAMILIES["clarify_definition"]
    if strategy == "supplement":
        return "supplement_context", EXPRESSION_FAMILIES["supplement_context"]
    if strategy == "deescalate":
        return "deescalate_uncertainty", EXPRESSION_FAMILIES["deescalate_uncertainty"]
    if strategy == "topic_shift":
        return "topic_bridge", EXPRESSION_FAMILIES["topic_bridge"]
    if strategy == "celebrate":
        return "celebrate_brief", EXPRESSION_FAMILIES["celebrate_brief"]
    return "general_expression", EXPRESSION_FAMILIES["general_expression"]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        for row in rows:
            stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
    os.chmod(temp, 0o600)
    temp.replace(path)


def compile_graph(source: Path, output: Path) -> dict:
    proposal_path = source / "behavior_proposals.jsonl"
    manifest_path = source / "manifest.json"
    if not proposal_path.is_file():
        raise ValueError("behavior proposal file is missing")
    proposals = [json.loads(line) for line in proposal_path.open(encoding="utf-8")]
    nodes: dict[str, dict] = {}
    edges: dict[str, dict] = {}
    support_cases: defaultdict[str, set[str]] = defaultdict(set)
    support_evidence: defaultdict[str, set[str]] = defaultdict(set)
    strategy_outcomes: defaultdict[str, Counter] = defaultdict(Counter)

    def node(node_type: str, identity_key: str, **body: object) -> str:
        identity = sid(node_type.lower(), clean(identity_key))
        item = nodes.setdefault(
            identity,
            {
                "id": identity,
                "type": node_type,
                "status": "candidate",
                "version": PIPELINE_VERSION,
                **body,
            },
        )
        item.setdefault("observations", 0)
        item["observations"] += 1
        # Repeated proposals share canonical Strategy/Act/Situation nodes.
        # Preserve all non-identifying list metadata instead of silently
        # keeping whichever batch happened to arrive first.
        for field in ("avoid", "cues", "topic_terms"):
            values = body.get(field)
            if not values:
                continue
            current = item.setdefault(field, [])
            item[field] = list(dict.fromkeys([*current, *values]))[:32]
        if body.get("raw_variants"):
            current = item.setdefault("raw_variants", [])
            item["raw_variants"] = list(dict.fromkeys([*current, *body["raw_variants"]]))[:20]
        return identity

    def edge(relation: str, source_id: str, target_id: str, evidence_id: str) -> None:
        identity = sid("edge", "|".join((relation, source_id, target_id, evidence_id)))
        edges.setdefault(
            identity,
            {
                "id": identity,
                "relation": relation,
                "source": source_id,
                "target": target_id,
                "evidence_id": evidence_id,
                "version": PIPELINE_VERSION,
            },
        )

    cases_seen: set[str] = set()
    for proposal in proposals:
        case_id = proposal["case_id"]
        evidence_key = case_id + "|" + "|".join(sorted(proposal.get("source_message_ids", [])))
        evidence_id = node(
            "Evidence",
            evidence_key,
            case_id=case_id,
            source_message_ids=sorted(set(proposal.get("source_message_ids", []))),
            source_kind="private_deidentified_corpus",
            required_message_ids=proposal.get("required_message_ids", []),
            selected_message_ids=proposal.get("selected_source_message_ids", proposal.get("source_message_ids", [])),
            abstracted_behavior=proposal.get("abstraction", ""),
        )
        # The expression family is needed by the case node as well as by the
        # ExpressionPattern node, so it is resolved once, here, rather than
        # after the case has already been written.
        expression_key, expression_label = expression_family(
            proposal.get("expression_pattern", ""),
            proposal["strategy"],
            proposal["dialogue_act"],
        )
        case_id_node = node(
            "InteractionCase",
            case_id,
            case_id=case_id,
            source_message_ids=sorted(set(proposal.get("source_message_ids", []))),
            outcome=proposal.get("outcome", "uncertain"),
            required_message_ids=proposal.get("required_message_ids", []),
            observable_cues=proposal.get("observable_cues", []),
            topic_terms=proposal.get("topic_terms", []),
            abstraction=proposal.get("abstraction", ""),
            strategy=proposal.get("strategy", ""),
            dialogue_act=proposal.get("dialogue_act", ""),
            # Both are case-level text the retriever scores. They were dropped
            # once and nothing noticed, because the shipped snapshot still had
            # them and no test rebuilt the graph from proposals.
            expression_pattern_text=proposal.get("expression_pattern", ""),
            expression_family=expression_key,
        )
        cases_seen.add(case_id)
        family_key, family_label = situation_family(
            proposal["situation"], proposal.get("observable_cues", [])
        )
        situation = node(
            "Situation",
            family_key,
            family_key=family_key,
            name=family_label,
            cues=proposal.get("observable_cues", []),
            raw_variants=[proposal["situation"]],
        )
        case_node = nodes[case_id_node]
        case_node["situation_text"] = proposal["situation"]
        case_node["situation_family"] = family_key
        strategy = node("Strategy", proposal["strategy"], key=proposal["strategy"])
        act = node("DialogueAct", proposal["dialogue_act"], key=proposal["dialogue_act"])
        # Keyed by the *expression family*, not by the raw wording. The node()
        # helper accumulates ``avoid`` and ``raw_variants`` across every case
        # that shares an identity, and the retriever reads exactly those two
        # fields (plus ``name``) — so keying by the raw text builds one node per
        # case, the accumulator never fires, and the retriever is handed
        # ExpressionPattern nodes with no ``name`` and no ``raw_variants`` to
        # match on. That regression was live: recompiling produced 2004
        # expression nodes against the shipped snapshot's 15.
        expression = node(
            "ExpressionPattern",
            expression_key,
            family_key=expression_key,
            name=expression_label,
            pattern=expression_label,
            raw_variants=[proposal["expression_pattern"]],
            avoid=proposal.get("avoid", []),
        )
        media = node("MediaIntent", proposal["media_intent"], key=proposal["media_intent"])
        edge("observed_in", case_id_node, situation, evidence_id)
        edge("supports", case_id_node, strategy, evidence_id)
        # These edges retain a case key.  Without it, one global Strategy
        # node would make unrelated cases appear to share every expression.
        edge("realizes_action", case_id_node, act, evidence_id)
        edge("expressed_as", case_id_node, expression, evidence_id)
        edge("may_use", case_id_node, media, evidence_id)
        edge("uses_in_case", strategy, act, evidence_id)
        edge("expressed_as_in_case", act, expression, evidence_id)
        edge("may_use_in_case", act, media, evidence_id)
        edge("has_evidence", case_id_node, evidence_id, evidence_id)
        for term in proposal.get("topic_terms", []):
            if len(clean(term)) < 2:
                continue
            concept = node("Concept", term, name=term, kind="topic_or_slang")
            edge("appears_in", concept, case_id_node, evidence_id)
        support_cases[strategy].add(case_id)
        support_evidence[strategy].add(evidence_id)
        strategy_outcomes[strategy][proposal.get("outcome", "uncertain")] += 1

    # Publication is deliberately conservative: repeated independent cases
    # become active candidates; one-off examples remain available for review.
    active = 0
    for identity, item in nodes.items():
        if item["type"] == "Strategy":
            item["support_case_count"] = len(support_cases[identity])
            item["support_evidence_count"] = len(support_evidence[identity])
            item["observed_outcomes"] = dict(strategy_outcomes[identity])
            if item["support_case_count"] >= 2:
                item["status"] = "active_candidate"
                active += 1
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    output.chmod(0o700)
    write_jsonl(output / "nodes.jsonl", list(nodes.values()))
    write_jsonl(output / "edges.jsonl", list(edges.values()))
    report = {
        "schema_version": 1,
        "pipeline_version": PIPELINE_VERSION,
        "source_manifest": json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else None,
        "proposals": len(proposals),
        "cases": len(cases_seen),
        "nodes": len(nodes),
        "edges": len(edges),
        "active_candidate_strategies": active,
        "node_types": dict(Counter(item["type"] for item in nodes.values())),
        "privacy": "private_deidentified_evidence_only_no_person_nodes",
        "status": "logical_graph_compiled",
    }
    temp = output / "manifest.json.tmp"
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, 0o600)
    temp.replace(output / "manifest.json")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(compile_graph(args.source, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
