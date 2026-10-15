"""Offline benchmark for behavior-graph retrieval.

Two evaluation protocols, because the one that shipped was measuring the wrong
thing:

``legacy_family``
    The original proxy. The "expected" label is ``situation_family(query)`` — a
    keyword table whose fallback bucket is ``general_group_exchange``. On the
    held-out splits that fallback covers 68% / 54.5% of queries, so a constant
    predictor scores far above the retrieval. Comparing a retrieved case's
    family against "we could not tell" is not a correctness test, and the number
    it produced (20.7% / 32.9%) cannot be read as retrieval quality. Kept here
    only for continuity.

``holdout_strategy``
    A real supervised task. Whole sessions are held out of the graph, so no
    sibling case shares the query's context; the query is the held-out case's own
    recent messages, and the ground truth is that case's own extracted strategy.
    This asks the question that matters: *given a situation we have never seen,
    does retrieval propose the strategy that actually fitted it?*

Every variant is scored by both protocols, alongside a majority-class baseline
that must be beaten for the variant to be worth anything.

Run from the repository root::

    .venv/bin/python -m tools.experiments.bench_behavior_retrieval --output experiments/behavior-retrieval.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from array import array
from collections import Counter, defaultdict
from pathlib import Path


from qqbot.agent.cognition.experience.graph_retrieval import BehaviorGraphRetriever
from qqbot.agent.cognition.experience.retrieval import _terms
from tools.experiments.compile_behavior_graph import situation_family

DEFAULT_GRAPH = Path("data/private/derived/behavior-graph-compiled-final-20261014-v8")
DEFAULT_CORPUS = Path("data/private/derived/behavior-corpus-20261014")
DEFAULT_CALIBRATION = Path(
    "data/private/derived/behavior-validation-final-20261014/calibration.json"
)
DEFAULT_OUTPUT = Path("experiments/behavior-retrieval.json")

#: Fold count for the session-level cross-validation. Every case is evaluated
#: exactly once, and each fold trains on a disjoint set of sessions, so the
#: evidence is multiplied without shrinking the training side.
DEFAULT_FOLDS = 5
#: How many recent messages make up a query, matching the online turn window.
QUERY_WINDOW = 10


def jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def fold_of(key: str, folds: int = DEFAULT_FOLDS) -> int:
    """Assign a session to a fold by stable hash.

    Hashing rather than slicing a list keeps the partition independent of file
    order, so re-running the compile step cannot silently reshuffle which
    sessions train and which are evaluated.
    """
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % max(1, folds)


def _share(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def mcnemar(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value for a paired comparison.

    Two accuracies on the same held-out cases cannot be compared with an
    unpaired test: the cases a variant fixes and the cases it breaks are
    correlated with each other. McNemar looks only at the disagreements, which
    is exactly the evidence that one ranking beat the other.
    """
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, min(b, c) + 1)) / (2**n)
    return round(min(1.0, 2 * tail), 4)


# --------------------------------------------------------------------------- #
# scoring variants
# --------------------------------------------------------------------------- #


def build_idf(retriever, postings: dict | None = None) -> dict[str, float]:
    """``log(N / df)`` over case paths, for terms that appear at least once.

    The current scorer counts overlapping terms, so a query full of common
    two-character n-grams scores every case about equally and the ranking is
    decided by noise. IDF is the standard correction and costs one pass.
    """
    postings = retriever._postings if postings is None else postings
    total = len(retriever.case_paths) or 1
    return {term: math.log(total / len(rows)) for term, rows in postings.items()}


def build_postings(retriever, pick) -> tuple[dict, dict]:
    """Build an inverted index over a *subset* of each path's fields.

    ``rank`` needs this to answer the one question that decides where to invest:
    is the retrieval text carrying the *interactional* signal (who did what, in
    what shape) or only the *topical* signal (what the room was talking about)?
    Restricting the index to one half at a time separates the two.
    """
    postings: defaultdict[str, list[int]] = defaultdict(list)
    for index, path in enumerate(retriever.case_paths):
        for term in _terms(pick(path)):
            postings[term].append(index)
    postings = {term: array("i", rows) for term, rows in postings.items()}
    hint_terms = {term for terms in retriever._hints.values() for term in terms}
    hint_postings = {
        term: set(postings[term]) for term in hint_terms if term in postings
    }
    return postings, hint_postings


def topic_text(path) -> str:
    """Only the topical half of a case: what the room was discussing."""
    case, _strategy, _action, _expression, _media, situation, _evidence = path
    return " ".join(
        [
            case.get("situation_text", ""),
            situation.get("name", ""),
            situation.get("family_key", ""),
            *case.get("topic_terms", []),
        ]
    )


def interaction_text(path) -> str:
    """Only the interactional half: what was done, and in what shape."""
    case, strategy, action, expression, _media, _situation, _evidence = path
    return " ".join(
        [
            *case.get("observable_cues", []),
            strategy.get("key", ""),
            action.get("key", ""),
            expression.get("name", ""),
            *expression.get("raw_variants", [])[:5],
        ]
    )


def _case_level(path) -> tuple:
    """Case-level text the shipped index never reads.

    These four fields live on the *case* node, not on the expression/situation
    nodes the field-split helpers walk, so they fell outside both halves and have
    never been scored. ``expression_pattern_text`` and ``abstraction`` are free
    text; ``expression_family`` and ``outcome`` are small categorical labels.
    """
    case = path[0]
    return (
        case.get("expression_pattern_text", ""),
        case.get("abstraction", ""),
        case.get("expression_family", ""),
        case.get("outcome", ""),
    )


def interaction_plus_pattern(path) -> str:
    """Interactional half plus the expression pattern's own prose."""
    return interaction_text(path) + " " + _case_level(path)[0]


def interaction_plus_abstraction(path) -> str:
    """Interactional half plus the one-sentence abstraction."""
    return interaction_text(path) + " " + _case_level(path)[1]


def interaction_plus_all(path) -> str:
    """Interactional half plus every unused case-level field."""
    return interaction_text(path) + " " + " ".join(_case_level(path))


def rank(
    retriever,
    query: str,
    *,
    limit: int = 3,
    idf: dict[str, float] | None = None,
    use_hints: bool = True,
    use_intent: bool = True,
    use_family: bool = True,
    aggregate: str = "strategy",
    support_tiebreak: bool = True,
    length_norm: bool = False,
    lengths: list[int] | None = None,
    average_length: float = 1.0,
    postings: dict | None = None,
    hint_postings: dict | None = None,
    prior: dict[str, float] | None = None,
    prior_weight: float = 0.0,
    family_prior: dict[str, dict[str, float]] | None = None,
) -> list[dict]:
    """Rank case paths for ``query`` under one scoring variant.

    ``idf=None`` reproduces the shipped scorer exactly (overlap *count*); passing
    an IDF table switches to summed term weights. The remaining flags ablate the
    hand-written boosts, the strategy-level aggregation, the ``support_case_count``
    tie-break and case-length normalisation — each one is a hypothesis about why
    the shipped ranking loses to a constant predictor.

    ``postings``/``hint_postings`` swap the index for one built over a subset of
    each path's fields, which is how the topical/interactional split is measured.

    ``prior``/``prior_weight`` blend the retrieval with the class prior as
    ``log(prior) + weight * normalised_score``. ``weight=0`` is exactly the
    constant predictor, so a sweep of weights answers the only question that
    matters before more tuning: does the retrieval know anything the base rate
    does not?
    """
    if not isinstance(query, str) or not query.strip():
        return []
    index = retriever._postings if postings is None else postings
    hint_index = retriever._hint_postings if hint_postings is None else hint_postings
    query_terms = _terms(query)
    intent = retriever._intent_bonus(query_terms) if use_intent else {}
    family = (
        retriever.calibration.get(retriever._query_family(query), {})
        if use_family
        else {}
    )

    # Terms each path matched, so the score can be a count or a sum of weights.
    matched: defaultdict[int, list[str]] = defaultdict(list)
    for term in query_terms:
        for index_of_case in index.get(term, ()):
            matched[index_of_case].append(term)

    def weight(terms, case_index: int) -> float:
        if idf is None:
            value = float(len(terms))
        else:
            value = sum(idf.get(term, 0.0) for term in terms)
        if length_norm and lengths:
            # Long cases carry more terms and so match more of any query. BM25's
            # length penalty is the standard fix; without it, the longest case
            # in the graph wins almost every query. ``b = 0.5`` as shipped in the
            # classic formula, so the divisor is >1 for long cases (penalty) and
            # <1 for short ones (boost).
            length = lengths[case_index] if case_index < len(lengths) else average_length
            value = value / (0.5 + 0.5 * length / average_length)
        return value

    rows: list[tuple[float, str, dict]] = []
    for case_index, (
        case,
        strategy,
        action,
        expression,
        media,
        situation,
        evidence,
    ) in enumerate(retriever.case_paths):
        terms = list(matched.get(case_index, ()))
        if use_hints:
            strategy_key = strategy.get("key", "")
            for term in retriever._hints.get(strategy_key, ()):
                if term in query_terms and case_index not in hint_index.get(term, ()):
                    terms.append(term)
        if not terms:
            continue
        strategy_key = strategy.get("key", "")
        score = (
            weight(terms, case_index)
            + intent.get(strategy_key, 0.0)
            + float(family.get(strategy_key, 0.0))
        )
        rows.append(
            (
                score,
                case.get("case_id", ""),
                {
                    "strategy": strategy_key,
                    "dialogue_act": action.get("key", "wait"),
                    "expression_pattern": expression.get("name", ""),
                    "situation_family": situation.get("family_key"),
                    "case_id": case.get("case_id"),
                    "topic_terms": case.get("topic_terms", []),
                    "support_case_count": int(
                        strategy.get("support_case_count", 0) or 0
                    ),
                },
            )
        )

    if aggregate == "case":
        # Rank individual cases and keep the first case of each strategy. The
        # shipped scorer instead averages a strategy's top rows, which lets a
        # large, bland strategy outrank a small, precise one.
        rows.sort(key=lambda item: (-item[0], item[1]))
        seen: set[str] = set()
        picked = []
        for score, case_id, payload in rows:
            key = payload["strategy"]
            if key in seen:
                continue
            seen.add(key)
            picked.append((score, payload))
            if len(picked) >= limit:
                break
        return [
            {**payload, "score": score, "top_row_score": score}
            for score, payload in picked
        ]

    grouped: defaultdict[str, list[tuple[float, str, dict]]] = defaultdict(list)
    for score, case_id, payload in rows:
        grouped[payload["strategy"]].append((score, case_id, payload))

    ranked = []
    for strategy_key, group in grouped.items():
        group.sort(key=lambda item: (-item[0], item[1]))
        best = group[0][2]
        average = sum(row[0] for row in group[:3]) / min(3, len(group))
        support = best["support_case_count"] if support_tiebreak else 0
        ranked.append(
            (
                average,
                support,
                strategy_key,
                {**best, "score": average, "top_row_score": group[0][0]},
            )
        )

    if prior is not None:
        # ``log(prior) + lambda * normalised_retrieval``. At lambda = 0 this is
        # the constant predictor; at large lambda it is the raw retrieval. If no
        # lambda in between beats lambda = 0, the retrieval carries no signal
        # beyond "how often does this strategy happen at all" — which is the
        # question that decides whether to keep tuning it or replace the labels.
        #
        # ``family_prior`` swaps the flat base rate for the base rate *within the
        # query's predicted situation family*. The situation layer's ceiling is
        # the ``situation_oracle`` (~41%), so conditioning on the predicted family
        # is the deployable way to capture part of that headroom: the prediction
        # is the same keyword heuristic the retriever already uses, not the truth.
        #
        # Note the guard is ``prior is not None``, not truthiness: lambda = 0 is
        # the single most important point in the sweep and must not be skipped
        # as if it were an unset value.
        active = prior
        if family_prior is not None:
            predicted = retriever._query_family(query)
            if predicted in family_prior:
                active = family_prior[predicted]
        ceiling = max((item[0] for item in ranked), default=0.0) or 1.0
        floor = math.log(max(active.values(), default=1.0) + 1.0)
        ranked = [
            (
                prior_weight * (average / ceiling) + math.log(active.get(key, 1e-6)) + floor,
                support,
                key,
                payload,
            )
            for average, support, key, payload in ranked
        ]

    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [item[3] for item in ranked[:limit]]


def variants(retriever) -> dict:
    """The scoring variants under test, as callables over a query.

    Four families of hypothesis:

    *Ablation* — is a hand-written boost helping or hurting?
    *IDF* — the shipped scorer counts overlapping n-grams, so common bigrams
    swamp rare, discriminative ones.
    *Ranking* — the shipped ranking collapses each strategy to the mean of its
    top three rows, then breaks ties on ``support_case_count``; either could let
    a large bland strategy outrank a small precise one.
    *Field split* — the decisive one. The compiled case text is mostly topical
    (``situation_text``, ``topic_terms``, situation name) while the *label* is
    interactional (what the bot did). If a topic-only index scores the same as
    the full one, the retrieval has no interactional signal at all and no amount
    of re-weighting will help.
    *Prior blend* — ``log(prior) + lambda * score``. ``lambda = 0`` is the
    constant predictor by construction, so if no weight in the sweep beats it,
    the retrieval is carrying nothing the base rate does not already say.
    """
    idf = build_idf(retriever)
    lengths = [len(_terms(retriever._searchable(path))) for path in retriever.case_paths]
    average_length = (sum(lengths) / len(lengths)) if lengths else 1.0

    topic_postings, topic_hints = build_postings(retriever, topic_text)
    topic_idf = build_idf(retriever, topic_postings)
    interaction_postings, interaction_hints = build_postings(retriever, interaction_text)
    interaction_idf = build_idf(retriever, interaction_postings)
    # The shipped index is the interactional half; these add the case-level
    # fields it has never read, to see whether they carry signal.
    pattern_postings, pattern_hints = build_postings(retriever, interaction_plus_pattern)
    pattern_idf = build_idf(retriever, pattern_postings)
    abstraction_postings, abstraction_hints = build_postings(
        retriever, interaction_plus_abstraction
    )
    abstraction_idf = build_idf(retriever, abstraction_postings)
    all_postings, all_hints = build_postings(retriever, interaction_plus_all)
    all_idf = build_idf(retriever, all_postings)

    strategy_counts = Counter(path[1].get("key", "") for path in retriever.case_paths)
    seen_cases = sum(strategy_counts.values()) or 1
    prior = {key: count / seen_cases for key, count in strategy_counts.items()}

    # The same base rate, sliced by situation family. The situation layer already
    # reaches ~41% when the family is known, so this is where the headroom over
    # the flat prior would show up — conditioned on the *predicted* family, which
    # is what a running bot actually has.
    family_counts: defaultdict[str, Counter] = defaultdict(Counter)
    for path in retriever.case_paths:
        family_counts[path[5].get("family_key") or "?"][path[1].get("key", "")] += 1
    family_prior = {
        family: {key: count / total for key, count in counts.items()}
        for family, counts in family_counts.items()
        for total in (sum(counts.values()) or 1,)
    }

    def r(q, **kwargs):
        return rank(retriever, q, lengths=lengths, average_length=average_length, **kwargs)

    def blended(weight):
        """The best-scoring variant so far, pulled toward the base rate."""
        return lambda q: r(
            q,
            idf=idf,
            use_hints=False,
            use_intent=False,
            use_family=False,
            prior=prior,
            prior_weight=weight,
        )

    def family_blended(weight):
        """Same blend, but the base rate is sliced by the predicted family."""
        return lambda q: r(
            q,
            idf=idf,
            use_hints=False,
            use_intent=False,
            use_family=False,
            prior=prior,
            prior_weight=weight,
            family_prior=family_prior,
        )

    def interaction_blended(weight):
        """The macro-optimal scorer, pulled toward the base rate.

        The interactional-cues index is the only variant that beats the raw
        scorer on *both* micro and macro accuracy, so blending *it* — rather than
        the full index — is the way to ask whether calibration and a better
        ranking can coexist.
        """
        return lambda q: r(
            q,
            postings=interaction_postings,
            hint_postings=interaction_hints,
            idf=interaction_idf,
            use_hints=False,
            use_intent=False,
            use_family=False,
            prior=prior,
            prior_weight=weight,
        )

    return {
        "baseline（现状）": lambda q: r(q),
        # --- ablation: which boost, if any, earns its keep ------------------- #
        "去掉 hint 并集": lambda q: r(q, use_hints=False),
        "去掉意图加分": lambda q: r(q, use_intent=False),
        "去掉校准加分": lambda q: r(q, use_family=False),
        "只留词重叠（无任何加分）": lambda q: r(
            q, use_hints=False, use_intent=False, use_family=False
        ),
        # --- IDF weighting --------------------------------------------------- #
        "IDF 加权": lambda q: r(q, idf=idf),
        "IDF 加权 + 意图加分": lambda q: r(q, idf=idf, use_hints=False, use_family=False),
        "IDF 加权（无任何加分）": lambda q: r(
            q, idf=idf, use_hints=False, use_intent=False, use_family=False
        ),
        # --- ranking: stop letting big bland strategies win on volume -------- #
        "排名字典序（不按支撑数打平）": lambda q: r(q, support_tiebreak=False),
        "案例级聚合（按单案例排序）": lambda q: r(q, aggregate="case"),
        "案例级聚合 + IDF": lambda q: r(q, idf=idf, aggregate="case"),
        # --- ranking: stop letting the longest case win every query ---------- #
        "长度归一": lambda q: r(q, length_norm=True),
        "长度归一 + IDF": lambda q: r(q, idf=idf, length_norm=True),
        # --- field split: where does the signal actually live? --------------- #
        "只看话题/情境文本": lambda q: r(
            q, postings=topic_postings, hint_postings=topic_hints, idf=topic_idf
        ),
        "只看互动线索文本": lambda q: r(
            q,
            postings=interaction_postings,
            hint_postings=interaction_hints,
            idf=interaction_idf,
        ),
        "只看互动线索（无加分）": lambda q: r(
            q,
            postings=interaction_postings,
            hint_postings=interaction_hints,
            idf=interaction_idf,
            use_hints=False,
            use_intent=False,
            use_family=False,
        ),
        # --- case-level fields the shipped index never reads ------------------ #
        "互动线索 + 表达结构文本": lambda q: r(
            q,
            postings=pattern_postings,
            hint_postings=pattern_hints,
            idf=pattern_idf,
            use_hints=False,
            use_intent=False,
            use_family=False,
        ),
        "互动线索 + 抽象说明": lambda q: r(
            q,
            postings=abstraction_postings,
            hint_postings=abstraction_hints,
            idf=abstraction_idf,
            use_hints=False,
            use_intent=False,
            use_family=False,
        ),
        "互动线索 + 案例级全部字段": lambda q: r(
            q,
            postings=all_postings,
            hint_postings=all_hints,
            idf=all_idf,
            use_hints=False,
            use_intent=False,
            use_family=False,
        ),
        # --- everything at once ---------------------------------------------- #
        "IDF + 案例级聚合 + 长度归一": lambda q: r(
            q, idf=idf, aggregate="case", length_norm=True
        ),
        "IDF + 案例级聚合 + 长度归一 + 无加分": lambda q: r(
            q,
            idf=idf,
            aggregate="case",
            length_norm=True,
            use_hints=False,
            use_intent=False,
            use_family=False,
        ),
        # --- prior blend: does the retrieval know anything the base rate does not? #
        # lambda = 0 is the constant predictor by construction, so it doubles as a
        # check that the sweep is wired correctly: it must land exactly on the
        # majority baseline.
        "先验校准 λ=0（全猜先验）": blended(0.0),
        "先验校准 λ=0.5": blended(0.5),
        "先验校准 λ=1": blended(1.0),
        "先验校准 λ=1.5": blended(1.5),
        "先验校准 λ=2": blended(2.0),
        "先验校准 λ=3": blended(3.0),
        "先验校准 λ=4": blended(4.0),
        "先验校准 λ=6": blended(6.0),
        "先验校准 λ=10": blended(10.0),
        # --- prior blend, conditioned on the predicted situation family --------- #
        # Same sweep, but the base rate is the one *inside* the query's predicted
        # family. If this beats the flat blend, the situation layer is where the
        # remaining headroom lives.
        "情境分组先验 λ=0": family_blended(0.0),
        "情境分组先验 λ=1.5": family_blended(1.5),
        "情境分组先验 λ=3": family_blended(3.0),
        # --- the macro-optimal scorer, blended --------------------------------- #
        # ``只看互动线索（无加分）`` is the only variant that beats the raw scorer on
        # both micro and macro accuracy. Blending *that* answers the remaining
        # question: can calibration and a better ranking coexist, or does
        # calibration only ever trade macro for micro?
        "互动线索+先验 λ=0": interaction_blended(0.0),
        "互动线索+先验 λ=0.5": interaction_blended(0.5),
        "互动线索+先验 λ=1": interaction_blended(1.0),
        "互动线索+先验 λ=2": interaction_blended(2.0),
    }


# --------------------------------------------------------------------------- #
# protocols
# --------------------------------------------------------------------------- #


def prepare(corpus: Path, graph: Path, calibration: Path):
    """Load the graph, the corpus and the join between them.

    The compiled case nodes do not carry ``session_id``; the corpus does. The
    join is what makes session-level holdout possible, which is what stops a
    held-out query from being answered by a sibling case built from the very
    same messages.
    """
    retriever = BehaviorGraphRetriever(graph, calibration=calibration)
    messages = {row["id"]: row for row in jsonl(corpus / "messages.jsonl")}
    corpus_cases = {row["id"]: row for row in jsonl(corpus / "training_candidates.jsonl")}
    return retriever, messages, corpus_cases


def query_for(case: dict, messages: dict, window: int = QUERY_WINDOW) -> str:
    rows = [messages[mid] for mid in case["context_ids"][-window:] if mid in messages]
    return "\n".join(str(row.get("text", ""))[:500] for row in rows)


def score_run(rows: list[dict], total: int) -> dict:
    covered = sum(1 for row in rows if row["strategies"])
    top1 = sum(1 for row in rows if row["strategies"] and row["strategies"][0] == row["truth"])
    any3 = sum(1 for row in rows if row["truth"] in row["strategies"])
    denominator = total or 1

    # Per-class recall *and precision*, because an aggregate number hides the
    # failure mode that matters: a system that only ever predicts the biggest
    # class scores exactly the base rate and looks "not terrible" while being
    # useless on everything else. Recall shows what is missed; precision shows
    # whether a prediction means anything at all — a class whose precision sits
    # at or below its own share is being predicted from the base rate, not from
    # the query.
    by_truth: defaultdict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_truth[row["truth"]].append(row)
    predicted = Counter(r["strategies"][0] for r in rows if r["strategies"])
    per_class = {
        key: {
            "support": len(subset),
            "share": round(len(subset) / denominator, 4),
            "recall_top1": round(
                sum(1 for r in subset if r["strategies"] and r["strategies"][0] == key)
                / len(subset),
                4,
            ),
            "recall_any3": round(
                sum(1 for r in subset if key in r["strategies"]) / len(subset), 4
            ),
            "predicted_share": _share(predicted.get(key, 0), sum(predicted.values())),
            "precision_top1": _share(
                sum(1 for r in subset if r["strategies"] and r["strategies"][0] == key),
                predicted.get(key, 0),
            ),
        }
        for key, subset in sorted(by_truth.items(), key=lambda kv: -len(kv[1]))
    }
    for key, stats in per_class.items():
        # Lift is the whole point of the column: above zero means a prediction of
        # this class carries information; at or below zero it is the base rate.
        stats["lift"] = round(stats["precision_top1"] - stats["share"], 4)

    # Macro averages. Top-1 accuracy is a *micro* number: with the majority
    # class at ~35% of the corpus a system that only ever predicts ``supplement``
    # looks respectable while being blind to the other twelve classes. Macro
    # recall (balanced accuracy) and macro F1 weight every class equally, so they
    # answer the question micro accuracy hides: does the ranking discriminate
    # rare classes at all, or is it just riding the base rate?
    recalls = [stats["recall_top1"] for stats in per_class.values()]
    balanced_accuracy = round(sum(recalls) / len(recalls), 4) if recalls else 0.0
    f1s = []
    for stats in per_class.values():
        precision = stats["precision_top1"]
        recall = stats["recall_top1"]
        f1s.append(0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall))
    macro_f1 = round(sum(f1s) / len(f1s), 4) if f1s else 0.0

    confusion = {
        truth: dict(
            Counter(
                row["strategies"][0]
                for row in subset
                if row["strategies"]
            ).most_common(3)
        )
        for truth, subset in sorted(by_truth.items(), key=lambda kv: -len(kv[1]))
    }

    return {
        "cases": total,
        "coverage_at_3": round(covered / denominator, 4),
        "top1_accuracy": round(top1 / denominator, 4),
        "any_at_3": round(any3 / denominator, 4),
        "balanced_accuracy": balanced_accuracy,
        "macro_f1": macro_f1,
        "top1_counts": dict(predicted.most_common()),
        "per_class": per_class,
        "confusion_top1": confusion,
    }


def run_holdout_strategy(
    retriever, messages, corpus_cases, folds: int = DEFAULT_FOLDS, taxonomy=None
) -> dict:
    """Session-level cross-validation; ground truth is each case's own strategy.

    A single 20% holdout leaves a few hundred test cases, which is not enough to
    separate a three-point difference from noise: the paired test runs on the
    *discordant* pairs only, and there are just a few dozen of those. Since the
    fold partition is over whole sessions, every case can be evaluated exactly
    once while each fold still trains on a disjoint set of sessions — the
    evidence multiplies without giving the retriever anything it would not have
    in production.

    ``taxonomy`` maps fine strategy keys to coarse behavior classes, applied to
    both the predictions and the truth. It defaults to the *shipped* taxonomy so
    the report describes the code that actually runs; ``None`` reproduces the
    original 13-class numbers.
    """

    def cls(key: str) -> str:
        return taxonomy.get(key, key) if taxonomy else key

    session_of = {case_id: row.get("session_id") for case_id, row in corpus_cases.items()}
    truth_of = {
        path[0].get("case_id"): cls(path[1].get("key", ""))
        for path in retriever.case_paths
    }
    # Queries depend on the case, not the fold, so build them once.
    queries = {
        case_id: query_for(case, messages)
        for case_id, case in corpus_cases.items()
        if case_id in truth_of
    }

    saved_paths = retriever.case_paths
    fold_of_case = {
        path[0].get("case_id"): fold_of(session_of.get(path[0].get("case_id"), ""), folds)
        for path in saved_paths
    }

    pooled: dict[str, list[dict]] = {}
    pooled_train: Counter = Counter()
    per_fold: list[dict] = []
    held_out_sessions: set[str] = set()
    constant_hits = oracle_hits = evaluated = 0
    conditionals: defaultdict[str, Counter] = defaultdict(Counter)

    for fold in range(folds):
        held = [path for path in saved_paths if fold_of_case.get(path[0].get("case_id")) == fold]
        if not held:
            continue
        retriever.case_paths = [
            path for path in saved_paths if fold_of_case.get(path[0].get("case_id")) != fold
        ]
        retriever._build_index()
        train_cases = len(retriever.case_paths)
        train_strategies = Counter(
            cls(path[1].get("key", "")) for path in retriever.case_paths
        )
        pooled_train.update(train_strategies)
        majority = train_strategies.most_common(1)[0][0] if train_strategies else ""

        for _case, strategy, _action, _expression, _media, situation, _evidence in retriever.case_paths:
            conditionals[situation.get("family_key") or "?"][
                cls(strategy.get("key", ""))
            ] += 1

        fold_rows: dict[str, list[dict]] = {}
        for name, fn in variants(retriever).items():
            rows = []
            for path in held:
                case_id = path[0].get("case_id")
                query = queries.get(case_id)
                if query is None:
                    continue
                rows.append(
                    {
                        "case_id": case_id,
                        "truth": truth_of.get(case_id, ""),
                        "strategies": [cls(option["strategy"]) for option in fn(query)],
                    }
                )
            fold_rows[name] = rows
            pooled.setdefault(name, []).extend(rows)

        for path in held:
            case_id = path[0].get("case_id")
            truth = truth_of.get(case_id, "")
            held_out_sessions.add(session_of.get(case_id, ""))
            evaluated += 1
            constant_hits += truth == majority
            oracle_hits += oracle_pick(conditionals, path[5].get("family_key")) == truth

        per_fold.append(
            {
                "fold": fold,
                "held_out_cases": len(held),
                "train_cases": train_cases,
                "majority": majority,
                "majority_on_holdout": round(
                    sum(1 for p in held if truth_of.get(p[0].get("case_id")) == majority)
                    / len(held),
                    4,
                ),
                "variants": {
                    name: score_run(rows, len(rows))["top1_accuracy"]
                    for name, rows in fold_rows.items()
                },
            }
        )

    retriever.case_paths = saved_paths
    retriever._build_index()

    results = {name: score_run(rows, len(rows)) for name, rows in pooled.items()}
    correct = {
        name: [
            bool(row["strategies"] and row["strategies"][0] == row["truth"])
            for row in rows
        ]
        for name, rows in pooled.items()
    }

    # The pure-prior variant is the reference every other variant is paired
    # against: it is the best a system can do without looking at the query.
    reference = next((name for name in results if name.startswith("先验校准 λ=0")), None)
    if reference is not None:
        base = correct[reference]
        for name, flags in correct.items():
            if name == reference or len(flags) != len(base):
                continue
            fixed = sum(1 for got, was in zip(flags, base) if got and not was)
            broke = sum(1 for got, was in zip(base, flags) if got and not was)
            results[name]["paired_vs_prior"] = {
                "reference": reference,
                "fixed": fixed,
                "broke": broke,
                "p_value": mcnemar(fixed, broke),
            }

    top_class, top_count = (
        pooled_train.most_common(1)[0] if pooled_train else ("", 0)
    )
    total_train = sum(pooled_train.values()) or 1

    return {
        "protocol": "holdout_strategy",
        "description": (
            f"按会话 {folds} 折交叉验证；query 为案例自己的近期消息；"
            "真值是它自己被抽取出的策略"
            + ("（已按线上标签体系合并为粗类）" if taxonomy else "（原始细类）")
        ),
        "taxonomy": (
            {
                "fine_classes": len(taxonomy),
                "coarse_classes": len(set(taxonomy.values())),
                "mapping": dict(sorted(taxonomy.items())),
            }
            if taxonomy
            else None
        ),
        "folds": folds,
        "held_out_sessions": len(held_out_sessions),
        "held_out_cases": evaluated,
        "graph_cases": len(saved_paths),
        "train_cases": (
            sum(fold["train_cases"] for fold in per_fold) // max(1, len(per_fold))
        ),
        "majority_class": top_class,
        "majority_baseline_top1": round(top_count / total_train, 4),
        "majority_baseline_on_holdout": _share(constant_hits, evaluated),
        "held_out_strategy_distribution": dict(
            Counter(
                truth_of.get(path[0].get("case_id"), "") for path in saved_paths
            ).most_common()
        ),
        "train_strategy_distribution": dict(pooled_train.most_common()),
        "situation_oracle": {
            "description": "若情境分类完全正确，图谱自身的情境→策略统计能达到的上限",
            "families": len(conditionals),
            "accuracy": _share(oracle_hits, evaluated),
            "per_family_top_strategy": {
                family: dict(counts.most_common(3))
                for family, counts in conditionals.items()
            },
        },
        "per_fold": per_fold,
        "variants": results,
    }


def oracle_pick(conditionals: dict, family: str | None) -> str:
    counts = conditionals.get(family or "?")
    return counts.most_common(1)[0][0] if counts else ""


def run_legacy_family(retriever, messages, corpus_cases, variant_fns: dict) -> dict:
    """The original proxy, reproduced so the two protocols can be compared."""
    results = {}
    for name, fn in variant_fns.items():
        rows = []
        for split in ("validation", "test"):
            for case in corpus_cases.values():
                if case.get("split") != split:
                    continue
                query = query_for(case, messages)
                options = fn(query)
                expected, _ = situation_family(query, [])
                rows.append(
                    {
                        "case_id": case["id"],
                        "split": split,
                        "truth": expected,
                        "strategies": [option["strategy"] for option in options],
                        "family": (
                            options[0]["situation_family"] if options else None
                        ),
                    }
                )
        total = len(rows) or 1
        family_top1 = sum(
            1 for row in rows if row["family"] and row["family"] == row["truth"]
        )
        families = Counter(row["truth"] for row in rows)
        majority, majority_count = families.most_common(1)[0]
        results[name] = {
            **score_run(rows, len(rows)),
            "family_top1": round(family_top1 / total, 4),
        }
        results[name]["majority_family"] = majority
        results[name]["majority_family_baseline"] = round(majority_count / total, 4)
    return {
        "protocol": "legacy_family",
        "description": "原代理指标：期望标签来自 query 的关键词表，兜底桶 general_group_exchange",
        "variants": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, default=DEFAULT_GRAPH)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--folds",
        type=int,
        default=DEFAULT_FOLDS,
        help=f"按会话交叉验证的折数（默认 {DEFAULT_FOLDS}）",
    )
    parser.add_argument(
        "--taxonomy",
        choices=("shipped", "none"),
        default="shipped",
        help=(
            "标签体系：shipped（默认）用线上 graph_retrieval.DEFAULT_TAXONOMY 把 13 个细类"
            "合并为 9 个粗类，报告与线上代码一致；none 复现原始 13 类数字"
        ),
    )
    args = parser.parse_args()

    taxonomy = None
    if args.taxonomy == "shipped":
        from qqbot.agent.cognition.experience.graph_retrieval import DEFAULT_TAXONOMY

        taxonomy = DEFAULT_TAXONOMY

    retriever, messages, corpus_cases = prepare(
        args.corpus, args.graph, args.calibration
    )
    fns = variants(retriever)
    # Hoisted rather than read inside the literal below: the protocols mutate
    # ``case_paths`` while they run, so a value read there would depend on
    # dictionary-evaluation order to stay correct.
    graph_cases = len(retriever.case_paths)

    report = {
        "schema_version": 1,
        "graph": str(args.graph),
        "corpus": str(args.corpus),
        "calibration": str(args.calibration) if args.calibration else None,
        "graph_cases": graph_cases,
        "protocols": [
            run_holdout_strategy(
                retriever, messages, corpus_cases, args.folds, taxonomy
            ),
            run_legacy_family(retriever, messages, corpus_cases, fns),
        ],
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {args.output}")

    for protocol in report["protocols"]:
        print(f"\n=== {protocol['protocol']} ===")
        print(f"    {protocol['description']}")
        if protocol["protocol"] == "holdout_strategy":
            print(
                f"    图谱 {protocol['graph_cases']} 案例 · "
                f"{protocol['folds']} 折 × 约 {protocol['train_cases']} 条训练；"
                f"共评测 {protocol['held_out_cases']} 条（{protocol['held_out_sessions']} 个会话）"
            )
            print(
                f"    常数预测器（全猜 {protocol['majority_class']}）："
                f"训练侧占 {protocol['majority_baseline_top1'] * 100:.1f}%，"
                f"在评测集上拿到 "
                f"{protocol['majority_baseline_on_holdout'] * 100:.1f}%"
            )
            oracle = protocol["situation_oracle"]
            print(
                f"    情境完美时的上限（{oracle['families']} 个情境族）= "
                f"{oracle['accuracy'] * 100:.1f}%"
            )
            for name, result in protocol["variants"].items():
                paired = result.get("paired_vs_prior")
                extra = ""
                if paired:
                    extra = (
                        f"  [修好 {paired['fixed']} / 弄坏 {paired['broke']}"
                        f" / p={paired['p_value']:.3f}]"
                    )
                print(
                    f"    {name:28s} top1={result['top1_accuracy'] * 100:5.1f}%  "
                    f"any@3={result['any_at_3'] * 100:5.1f}%{extra}"
                )
            baseline = next(iter(protocol["variants"].values()))
            print("    每折 Top-1（方向一致才说明不是某一折的偶然）:")
            for fold in protocol["per_fold"]:
                peak_name, peak_value = max(
                    fold["variants"].items(), key=lambda item: item[1]
                )
                pure = fold["variants"].get("先验校准 λ=0（全猜先验）")
                edge = (
                    f"{'↑' if pure is not None and peak_value > pure else '↓'}"
                    if pure is not None
                    else " "
                )
                print(
                    f"      折 {fold['fold']}: 常数预测器 "
                    f"{fold['majority_on_holdout'] * 100:5.1f}%   "
                    f"最优 {peak_name} {peak_value * 100:5.1f}%  {edge}"
                )
            print(
                "    分类召回/精确率（baseline）:  "
                "召回高于占比说明这一类被认出来了；精确率高于占比说明"
                "「预测成这一类」本身带信息"
            )
            for key, stats in list(baseline["per_class"].items())[:8]:
                print(
                    f"      {key:16s} 占比 {stats['share'] * 100:5.1f}%  "
                    f"top1 召回 {stats['recall_top1'] * 100:5.1f}%  "
                    f"精确 {stats['precision_top1'] * 100:5.1f}%  "
                    f"提升 {stats['lift'] * 100:+5.1f}pt"
                )
        else:
            for name, result in protocol["variants"].items():
                print(
                    f"    {name:28s} family={result['family_top1'] * 100:5.1f}%  "
                    f"（多数族 {result['majority_family_baseline'] * 100:5.1f}%）"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
