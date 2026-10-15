"""Read-only projection of the offline behavior-graph experiments.

``tools/experiments/bench_behavior_retrieval.py`` writes a raw report: for every scoring
variant, three numbers and a per-class breakdown. What the console needs is the
*reading* of that report — which variant beat which, where the matching signal
actually lives, and whether the retrieval beats a constant predictor.

That reading is derived here rather than in the browser for two reasons. The
console section and the standalone HTML page both render it, so deriving it once
is what stops them drifting apart; and deriving it in Python makes every
conclusion in the dashboard testable.

Nothing here is on the bot's reply path. It reads one JSON file that an operator
produced by hand, and it never writes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Presentation order of the variants, and the hypothesis each group tests.
#: Anything not listed still renders, appended to "其他" — a new experiment must
#: not disappear just because this table was not updated.
GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "基准",
        "现在线上真正在跑的那套打分",
        ("baseline（现状）",),
    ),
    (
        "消融：逐项关掉手写加分",
        "这些加分是人工写的启发式。关掉才能看出它帮忙还是添乱。",
        ("去掉 hint 并集", "去掉意图加分", "去掉校准加分", "只留词重叠（无任何加分）"),
    ),
    (
        "IDF 词频加权",
        "现状按「命中了几个 2/3-gram」计分，常见的二字组合会淹没稀有的判别性词。",
        ("IDF 加权", "IDF 加权 + 意图加分", "IDF 加权（无任何加分）"),
    ),
    (
        "排序方式：谁该排在前面",
        "现状把每个策略压成「它最好的 3 条案例的均分」，再用 support_case_count 打平；"
        "大而平庸的策略因此可能压过小而精准的策略。",
        (
            "排名字典序（不按支撑数打平）",
            "案例级聚合（按单案例排序）",
            "案例级聚合 + IDF",
            "长度归一",
            "长度归一 + IDF",
        ),
    ),
    (
        "字段拆分：信号到底在哪一半",
        "案例文本一半是「话题」（在聊什么），一半是「互动」（做了什么、什么形态）。"
        "标签是互动侧的。把索引限制到其中一半，就能看出检索到底靠哪半边在匹配。",
        ("只看话题/情境文本", "只看互动线索文本", "只看互动线索（无加分）"),
    ),
    (
        "案例级字段：索引从没读过的文本",
        "expression_pattern_text / abstraction / expression_family / outcome 都是案例级字段，"
        "现有索引一个都没读。加进去看是否携带信号。",
        ("互动线索 + 表达结构文本", "互动线索 + 抽象说明", "互动线索 + 案例级全部字段"),
    ),
    (
        "组合",
        "把上面有效的几项叠起来",
        ("IDF + 案例级聚合 + 长度归一", "IDF + 案例级聚合 + 长度归一 + 无加分"),
    ),
    (
        "先验校准：检索与先验博弈",
        "λ=0 就是「永远回答最大的那个策略」，是构造出来的常数预测器。"
        "如果整个扫描里没有任何 λ 超过它，说明检索没有提供先验之外的信息。",
        (
            "先验校准 λ=0（全猜先验）",
            "先验校准 λ=0.5",
            "先验校准 λ=1",
            "先验校准 λ=1.5",
            "先验校准 λ=2",
            "先验校准 λ=3",
            "先验校准 λ=4",
            "先验校准 λ=6",
            "先验校准 λ=10",
        ),
    ),
)

#: The prior-blend sweep, as (variant name prefix, weight).
SWEEP_PREFIX = "先验校准 λ="

#: Per-protocol reading instructions. ``legacy_family`` scores a different
#: metric, and its ``top1_accuracy`` is structurally zero — the truth there is a
#: situation family, so comparing it to a strategy string can never match. The
#: projection therefore swaps which column carries the signal.
PROTOCOL_METRICS = {
    "holdout_strategy": {
        "primary": "top1_accuracy",
        "primary_label": "Top-1 命中",
        "primary_help": "检索建议的策略，和这条案例真实用过的策略是否一致",
        "secondary": "any_at_3",
        "secondary_label": "任意@3",
        "secondary_help": "正确策略是否出现在前 3 个建议里",
        "show_coverage": True,
    },
    "legacy_family": {
        "primary": "family_top1",
        "primary_label": "情境族命中",
        "primary_help": "检索到案例的情境族，和关键词表给出的期望族是否一致",
        "secondary": None,
        "secondary_label": "",
        "secondary_help": "",
        "show_coverage": False,
    },
}

BETTER, WORSE, SAME = "better", "worse", "same"
GOOD, BAD, WARN, INFO = "good", "bad", "warn", "info"

COMMAND = ".venv/bin/python -m tools.experiments.bench_behavior_retrieval"


def _resolve_report_path(explicit: Path | str | None = None) -> Path:
    """Find the report relative to the working directory, then the repo root.

    The bot is normally launched from the repository root, but the console is
    also opened from a checkout where the working directory is elsewhere. Trying
    both keeps the section working without adding another environment variable.
    """
    if explicit is not None:
        return Path(explicit)
    candidates = [Path("experiments/behavior-retrieval.json")]
    for ancestor in Path(__file__).resolve().parents:
        if (ancestor / "pyproject.toml").is_file() and (ancestor / "src/qqbot").is_dir():
            candidates.append(ancestor / "experiments/behavior-retrieval.json")
            break
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return candidates[0]


def _share(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def _level(value: float, reference: float) -> str:
    if value > reference:
        return BETTER
    if value < reference:
        return WORSE
    return SAME


class ExperimentConsoleQuery:
    """Project the benchmark report into the shape the console renders."""

    def __init__(self, report_path: Path | str | None = None):
        self.report_path = _resolve_report_path(report_path)

    def behavior_retrieval(self) -> dict[str, Any]:
        try:
            report = json.loads(self.report_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return self._missing(
                "还没有实验数据",
                "先在仓库根目录跑一次基准测试，它会把结果写到 "
                "experiments/behavior-retrieval.json。",
            )
        except (OSError, json.JSONDecodeError) as error:
            return self._missing(
                "实验数据读取失败",
                f"{type(error).__name__}: {error}",
            )

        protocols = [
            projection
            for protocol in report.get("protocols", [])
            if (projection := _project_protocol(protocol)) is not None
        ]
        primary = protocols[0] if protocols else None
        return {
            "available": True,
            "path": str(self.report_path),
            "schema_version": report.get("schema_version"),
            "graph_cases": report.get("graph_cases"),
            "graph": report.get("graph"),
            "corpus": report.get("corpus"),
            "headline": _headline(primary),
            "findings": _findings(primary),
            "protocols": protocols,
        }

    def _missing(self, reason: str, detail: str) -> dict[str, Any]:
        return {
            "available": False,
            "reason": reason,
            "detail": detail,
            "path": str(self.report_path),
            "command": COMMAND,
        }


# --------------------------------------------------------------------------- #
# per-protocol projection
# --------------------------------------------------------------------------- #


def _project_protocol(protocol: dict) -> dict | None:
    variants = protocol.get("variants") or {}
    if not variants:
        return None
    kind = protocol.get("protocol", "")
    metrics = PROTOCOL_METRICS.get(kind, PROTOCOL_METRICS["holdout_strategy"])
    primary_key = metrics["primary"]
    baseline_name = next(iter(variants))
    baseline = variants[baseline_name]
    baseline_value = float(baseline.get(primary_key) or 0.0)

    if kind == "holdout_strategy":
        # The bar is what a constant predictor *achieves on the held-out set*,
        # not the training share it is derived from. The two differ — here by
        # several points — and using the training share as the bar makes the
        # retrieval look worse than it is.
        reference_value = float(
            protocol.get("majority_baseline_on_holdout")
            or protocol.get("majority_baseline_top1")
            or 0.0
        )
        reference_label = f"常数预测器（全猜 {protocol.get('majority_class', '?')}）"
        train_share = float(protocol.get("majority_baseline_top1") or 0.0)
        reference_note = (
            f"这是「永远回答同一个策略」在留出集上实际拿到的分数。"
            f"该策略在训练侧占 {train_share * 100:.1f}%，"
            f"但留出集的分布不同，所以实际只能拿到 {reference_value * 100:.1f}%。"
        )
        headline = (
            f"留出 {protocol.get('held_out_sessions', 0)} 个会话 / "
            f"{protocol.get('held_out_cases', 0)} 条案例，训练侧 "
            f"{protocol.get('train_cases', 0)} 条"
        )
    else:
        reference_value = float(baseline.get("majority_family_baseline") or 0.0)
        reference_label = (
            f"多数族基线（全猜 {baseline.get('majority_family', '?')}）"
        )
        reference_note = (
            "这个协议的期望标签来自关键词表，兜底桶占比过半，本身就不是正确性标准。"
        )
        headline = "验证集 + 测试集全部 query，复现旧指标以便对照"

    maximum = max(
        (float(row.get(primary_key) or 0.0) for row in variants.values()), default=0.0
    )
    rows = []
    for name, row in variants.items():
        value = float(row.get(primary_key) or 0.0)
        secondary = (
            float(row.get(metrics["secondary"]) or 0.0)
            if metrics["secondary"]
            else None
        )
        rows.append(
            {
                "name": name,
                "primary": round(value, 4),
                "secondary": None if secondary is None else round(secondary, 4),
                "balanced": round(float(row.get("balanced_accuracy") or 0.0), 4),
                "macro_f1": round(float(row.get("macro_f1") or 0.0), 4),
                "coverage": (
                    round(float(row.get("coverage_at_3") or 0.0), 4)
                    if metrics["show_coverage"]
                    else None
                ),
                "delta": round(value - baseline_value, 4),
                "level": _level(value, baseline_value),
                "is_baseline": name == baseline_name,
                "beats_reference": value > reference_value,
                "paired": _paired(row),
            }
        )

    best = max(rows, key=lambda row: row["primary"])
    sweep = _sweep(rows)
    return {
        "id": kind,
        "title": (
            "真实留出评估" if kind == "holdout_strategy" else "旧指标复现"
        ),
        "description": protocol.get("description", ""),
        "headline": headline,
        "metric": {
            "primary_label": metrics["primary_label"],
            "primary_help": metrics["primary_help"],
            "secondary_label": metrics["secondary_label"],
            "secondary_help": metrics["secondary_help"],
            "show_coverage": metrics["show_coverage"],
        },
        "reference": {
            "value": round(reference_value, 4),
            "label": reference_label,
            "note": reference_note,
            "train_value": (
                round(train_share, 4) if kind == "holdout_strategy" else None
            ),
        },
        "baseline": {"name": baseline_name, "value": round(baseline_value, 4)},
        "best": {
            "name": best["name"],
            "value": best["primary"],
            "delta": best["delta"],
            "beats_reference": best["beats_reference"],
            "paired": best.get("paired"),
        },
        "maximum": round(maximum, 4),
        "groups": _grouped(rows),
        "sweep": sweep,
        "per_class": _per_class(variants.get(baseline_name, {}), baseline_name),
        "per_class_best": _per_class(variants.get(best["name"], {}), best["name"]),
        "prediction_shift": _prediction_shift(
            protocol,
            baseline_name,
            variants.get(baseline_name, {}),
            best["name"],
            variants.get(best["name"], {}),
        ),
        "oracle": _oracle(protocol),
        "counts": {
            "folds": protocol.get("folds"),
            "held_out_sessions": protocol.get("held_out_sessions"),
            "held_out_cases": protocol.get("held_out_cases"),
            "train_cases": protocol.get("train_cases"),
        },
        "taxonomy": protocol.get("taxonomy"),
        "per_fold": _per_fold(protocol),
        "macro": _macro(rows, protocol),
    }


def _grouped(rows: list[dict]) -> list[dict]:
    by_name = {row["name"]: row for row in rows}
    seen: set[str] = set()
    groups = []
    for title, blurb, names in GROUPS:
        present = [by_name[name] for name in names if name in by_name]
        if present:
            groups.append({"title": title, "blurb": blurb, "rows": present})
            seen.update(row["name"] for row in present)
    leftover = [row for row in rows if row["name"] not in seen]
    if leftover:
        groups.append({"title": "其他", "blurb": "", "rows": leftover})
    return groups


def _per_class(variant: dict, variant_name: str) -> dict:
    classes = variant.get("per_class") or {}
    return {
        "variant": variant_name,
        "rows": [
            {
                "strategy": key,
                "support": stats.get("support", 0),
                "share": stats.get("share", 0.0),
                "recall_top1": stats.get("recall_top1", 0.0),
                "recall_any3": stats.get("recall_any3", 0.0),
                "predicted_share": stats.get("predicted_share", 0.0),
                "precision_top1": stats.get("precision_top1", 0.0),
                "lift": stats.get("lift", 0.0),
            }
            for key, stats in classes.items()
        ],
    }


def _per_fold(protocol: dict) -> list[dict]:
    """Per-fold accuracy of the constant predictor and of each fold's own best.

    Cross-validation can hide an unstable result: a pooled number that comes
    from one good fold and four bad ones looks the same as a consistent win.
    The per-fold rows are what make that visible.
    """
    rows = []
    for fold in protocol.get("per_fold") or []:
        variants = fold.get("variants") or {}
        peak_name, peak_value = (
            max(variants.items(), key=lambda item: item[1]) if variants else ("", 0.0)
        )
        pure = variants.get("先验校准 λ=0（全猜先验）")
        rows.append(
            {
                "fold": fold.get("fold"),
                "held_out_cases": fold.get("held_out_cases", 0),
                "train_cases": fold.get("train_cases", 0),
                "majority_class": fold.get("majority", ""),
                "constant": fold.get("majority_on_holdout", 0.0),
                "best_name": peak_name,
                "best": peak_value,
                "beats_constant": pure is not None and peak_value > pure,
                "edge": round(peak_value - pure, 4) if pure is not None else None,
            }
        )
    return rows


def _paired(variant: dict) -> dict | None:
    """The paired comparison against the constant predictor, if it was run."""
    paired = variant.get("paired_vs_prior")
    if not paired:
        return None
    p_value = paired.get("p_value", 1.0)
    return {
        "reference": paired.get("reference", ""),
        "fixed": paired.get("fixed", 0),
        "broke": paired.get("broke", 0),
        "p_value": p_value,
        "significant": p_value < 0.05,
    }


def _sweep(rows: list[dict]) -> list[dict]:
    """The prior-blend sweep as an ordered series, for the calibration curve."""
    series = []
    for row in rows:
        if not row["name"].startswith(SWEEP_PREFIX):
            continue
        label = row["name"][len(SWEEP_PREFIX) :].split("（")[0]
        try:
            weight = float(label)
        except ValueError:
            continue
        series.append(
            {
                "weight": weight,
                "name": row["name"],
                "primary": row["primary"],
                "secondary": row["secondary"],
                "paired": row.get("paired"),
            }
        )
    return sorted(series, key=lambda item: item["weight"])


def _prediction_shift(
    protocol: dict,
    baseline_name: str,
    baseline: dict,
    best_name: str,
    best: dict,
) -> dict:
    """How often each strategy is *predicted*, against how often it is *true*.

    A variant that gains accuracy purely by predicting the biggest class more
    often has not learned anything; this table is what makes that visible.
    """
    prior = protocol.get("train_strategy_distribution") or {}
    total = sum(prior.values()) or 1
    baseline_counts = baseline.get("top1_counts") or {}
    best_counts = best.get("top1_counts") or {}
    predicted_total = sum(baseline_counts.values()) or 1
    rows = [
        {
            "strategy": key,
            "prior": _share(count, total),
            "baseline": _share(baseline_counts.get(key, 0), predicted_total),
            "best": _share(best_counts.get(key, 0), predicted_total),
        }
        for key, count in prior.items()
    ]
    return {
        "baseline_variant": baseline_name,
        "best_variant": best_name,
        "rows": rows,
    }


def _macro(rows: list[dict], protocol: dict) -> dict | None:
    """The micro/macro divergence: does the ranking discriminate rare classes?

    Micro accuracy is dominated by the majority class. A system that only ever
    predicts ``supplement`` scores the base rate (~35%) while being blind to the
    other twelve classes — and its macro recall collapses to ``1/n``. The two
    metrics can therefore point in *opposite* directions, and on this corpus they
    do: the variant that maximises top-1 is not the variant that maximises macro
    recall. Which one to optimise depends on what the hint is for; the point of
    this block is that the choice is visible instead of hidden behind one number.
    """
    if not rows:
        return None
    classes = len(protocol.get("held_out_strategy_distribution") or {}) or 1
    constant_macro = round(1 / classes, 4)
    best_micro = max(rows, key=lambda row: row["primary"])
    best_macro = max(rows, key=lambda row: row["balanced"])
    baseline = next((row for row in rows if row["is_baseline"]), rows[0])
    return {
        "classes": classes,
        "constant_macro": constant_macro,
        "baseline_balanced": baseline["balanced"],
        "baseline_macro_f1": baseline["macro_f1"],
        "best_macro_name": best_macro["name"],
        "best_macro": best_macro["balanced"],
        "best_macro_f1": best_macro["macro_f1"],
        "best_micro_name": best_micro["name"],
        "best_micro": best_micro["primary"],
        "diverges": best_macro["name"] != best_micro["name"],
    }


def _oracle(protocol: dict) -> dict | None:
    oracle = protocol.get("situation_oracle")
    if not oracle:
        return None
    return {
        "accuracy": oracle.get("accuracy", 0.0),
        "families": oracle.get("families", 0),
        "description": oracle.get("description", ""),
        "per_family_top_strategy": oracle.get("per_family_top_strategy") or {},
        "held_out_family_distribution": oracle.get("held_out_family_distribution") or {},
    }


# --------------------------------------------------------------------------- #
# headline and conclusions
# --------------------------------------------------------------------------- #


def _headline(primary: dict | None) -> dict:
    """The four headline numbers, or nothing at all.

    Only the held-out protocol has a majority-class bar, a held-out count and a
    situation ceiling. A report whose first protocol is the legacy reproduction
    gets no headline rather than a row of blanks that look like failures.
    """
    if primary is None or primary["id"] != "holdout_strategy":
        return {}
    counts = primary.get("counts") or {}
    return {
        "protocol": primary["title"],
        "baseline_name": primary["baseline"]["name"],
        "baseline_value": primary["baseline"]["value"],
        "best_name": primary["best"]["name"],
        "best_value": primary["best"]["value"],
        "best_delta": primary["best"]["delta"],
        "reference_value": primary["reference"]["value"],
        "reference_label": primary["reference"]["label"],
        "beats_reference": primary["best"]["beats_reference"],
        "oracle_value": (primary["oracle"] or {}).get("accuracy"),
        "oracle_families": (primary["oracle"] or {}).get("families"),
        "reference_train_value": primary["reference"].get("train_value"),
        "best_paired": primary["best"].get("paired"),
        "best_significant": bool((primary["best"].get("paired") or {}).get("significant")),
        "held_out_cases": counts.get("held_out_cases"),
        "held_out_sessions": counts.get("held_out_sessions"),
        "train_cases": counts.get("train_cases"),
        "macro": primary.get("macro"),
        "taxonomy": primary.get("taxonomy"),
    }


def _row_for(primary: dict, name: str) -> dict | None:
    for group in primary["groups"]:
        for row in group["rows"]:
            if row["name"] == name:
                return row
    return None


def _findings(primary: dict | None) -> list[dict]:
    """The reading of the experiment, as actionable statements.

    Each finding is one claim, the numbers it rests on, and what to do about it.
    The point of the dashboard is that these can be re-derived after the next
    run instead of being remembered.

    The order is the order the questions should be asked in: does it beat doing
    nothing, is the win big enough to matter, is it stable across folds, what
    does it get right, and where is the remaining headroom.
    """
    if primary is None or primary["id"] != "holdout_strategy":
        return []

    baseline = primary["baseline"]
    best = primary["best"]
    reference = primary["reference"]
    oracle = primary["oracle"] or {}
    findings: list[dict] = []

    # 0. Reframe: micro and macro disagree, and the disagreement is the point. #
    # Top-1 accuracy is dominated by the majority class, so "the retrieval is
    # worse than doing nothing" and "the retrieval discriminates twice as well as
    # the constant predictor" are both true — of different metrics. This finding
    # leads because it changes how every number below should be read.
    macro = primary.get("macro")
    if macro and macro["best_macro"] >= macro["constant_macro"] * 1.5:
        findings.append(
            {
                "level": GOOD,
                "title": "换一个指标，结论就反了：检索确实能区分稀有类",
                "detail": (
                    f"Top-1 被 {_p(reference['value'])} 的多数类主导，"
                    "「只猜多数类」因此看起来还行；但它的平衡准确率只有 "
                    f"{_p(macro['constant_macro'])}（{macro['classes']} 类里只答对 1 类）。"
                    f"检索的平衡准确率最高到 {_p(macro['best_macro'])}"
                    f"（「{macro['best_macro_name']}」），是常数预测器的 "
                    f"{macro['best_macro'] / macro['constant_macro']:.1f} 倍。"
                ),
                "action": (
                    "先想清楚这个提示是干什么用的：要「点名最可能的那一步」，"
                    "就看 Top-1（先验校准）；要「给出与情境相称的多样建议」，"
                    "就看平衡准确率——此时先验校准反而有害。默认已按后者配置。"
                ),
            }
        )

    # 1. Does anything beat doing nothing? ---------------------------------- #
    best_paired = best.get("paired") or {}
    folds = primary.get("per_fold") or []
    agree = [fold for fold in folds if fold.get("beats_constant")]
    if best["beats_reference"] and best_paired.get("significant"):
        findings.append(
            {
                "level": GOOD,
                "title": "检索确实超过了「永远回答同一个策略」",
                "detail": (
                    f"最优变体「{best['name']}」的 Top-1 是 {_p(best['value'])}，"
                    f"高于 {reference['label']} 的 {_p(reference['value'])}。"
                    f"配对检验：修好 {best_paired.get('fixed')} 条、"
                    f"弄坏 {best_paired.get('broke')} 条，"
                    f"p={best_paired.get('p_value'):.3f}。"
                ),
                "action": (
                    "这是第一次有统计支撑的正向结论，可以把它当作新基准。"
                    "但注意下面一条：收益很小，而且必须先做先验校准才拿得到。"
                ),
            }
        )
    elif best["beats_reference"]:
        findings.append(
            {
                "level": WARN,
                "title": "超过了常数预测器，但差异还达不到统计显著",
                "detail": (
                    f"最优变体「{best['name']}」的 Top-1 是 {_p(best['value'])}，"
                    f"比 {reference['label']} 的 {_p(reference['value'])} 高 "
                    f"{_pt(best['value'] - reference['value'])}；"
                    f"配对检验修好 {best_paired.get('fixed')} 条、"
                    f"弄坏 {best_paired.get('broke')} 条，"
                    f"p={best_paired.get('p_value'):.3f}。"
                ),
                "action": "不要据此宣布图谱有效；先增加评测样本再判断。",
            }
        )
    else:
        findings.append(
            {
                "level": BAD,
                "title": "没有任何变体超过常数预测器",
                "detail": (
                    f"最优变体「{best['name']}」的 Top-1 是 {_p(best['value'])}，"
                    f"仍低于 {reference['label']} 的 {_p(reference['value'])}；"
                    f"现状基准是 {_p(baseline['value'])}。"
                ),
                "action": (
                    "不要再调打分权重。唯一剩下的方向是换掉「策略」这套标签，"
                    "让同一情境下真的存在可区分的选项。"
                ),
            }
        )

    # 2. Is the win big enough to matter, and what does it cost to get it? --- #
    # The single most actionable number in the report: every un-calibrated
    # variant scores *worse than doing nothing*, by a margin the paired test
    # calls decisive. Prior calibration is not a refinement of the retrieval,
    # it is the thing that makes the retrieval usable at all.
    uncalibrated = [
        (row["name"], row)
        for group in primary["groups"]
        for row in group["rows"]
        if not row["name"].startswith(SWEEP_PREFIX)
    ]
    hurt = [
        (name, row)
        for name, row in uncalibrated
        if (row.get("paired") or {}).get("significant")
        and row["primary"] < reference["value"]
    ]
    if hurt and best["beats_reference"]:
        worst = min(hurt, key=lambda item: item[1]["primary"])
        findings.append(
            {
                "level": WARN,
                "title": "收益很小，而且不校准的话检索比不做还差",
                "detail": (
                    f"最优配置只比常数预测器高 "
                    f"{_pt(best['value'] - reference['value'])}（约 "
                    f"{round((best['value'] - reference['value']) * (primary.get('counts') or {}).get('held_out_cases', 0))}"
                    " 条案例）。"
                    f"而所有不做先验校准的变体都显著更差："
                    f"共 {len(hurt)} 个，最差的「{worst[0]}」只有 "
                    f"{_p(worst[1]['primary'])}（p="
                    f"{(worst[1].get('paired') or {}).get('p_value'):.3f}）。"
                    f"现状打分是 {_p(baseline['value'])}。"
                ),
                "action": (
                    "先验校准不是可选优化，是让检索可用的前提——"
                    f"它把现状的 {_p(baseline['value'])} 拉到 {_p(best['value'])}，"
                    f"{_pt(best['value'] - baseline['value'])}，"
                    "且不需要新数据或新标签。先落地这一条，"
                    "再谈要不要为了剩下的几个点去重做标签层。"
                ),
            }
        )

    # 3. Is the result stable, or one lucky fold? --------------------------- #
    if len(folds) > 1 and agree:
        per_fold = "、".join(
            f"折{fold['fold']} {_p(fold['best'])} vs {_p(fold['constant'])}"
            for fold in folds
        )
        findings.append(
            {
                "level": GOOD if len(agree) == len(folds) else WARN,
                "title": (
                    f"{len(folds)} 折里 {len(agree)} 折方向一致"
                    if len(agree) == len(folds)
                    else f"只有 {len(folds)} 折里的 {len(agree)} 折方向一致"
                ),
                "detail": f"每折最优 vs 该折常数预测器：{per_fold}。",
                "action": (
                    "方向一致说明这不是某一折的偶然；"
                    "如果某一折反向，说明结论依赖于划分方式，需要更多会话再判断。"
                ),
            }
        )

    # 4. What does it actually get right? ---------------------------------- #
    # Aggregate accuracy can be flat while every class is individually
    # informative. Precision above a class's own share is what "the retrieval
    # knows something about this class" looks like.
    classes = (primary.get("per_class") or {}).get("rows") or []
    lifted = [row for row in classes if row.get("lift", 0) > 0.02]
    if lifted:
        names = "、".join(
            f"{row['strategy']} {row['lift'] * 100:+.1f}pt" for row in lifted[:5]
        )
        findings.append(
            {
                "level": INFO,
                "title": f"逐类看，{len(lifted)}/{len(classes)} 个策略的精确率高于自身占比",
                "detail": (
                    f"{names}。精确率高于占比意味着「预测成这一类」本身就带信息，"
                    "而不是靠先验凑数。"
                ),
                "action": (
                    "检索不是整体无效，而是被几个大而模糊的策略稀释了。"
                    "重做标签层时优先保住这几类的可分性。"
                ),
            }
        )

    # 5. Where does the matching signal live? ------------------------------- #
    topic = _row_for(primary, "只看话题/情境文本")
    interaction = _row_for(primary, "只看互动线索文本")
    if topic and interaction:
        best_half, worse_half = sorted(
            (topic, interaction), key=lambda row: -row["primary"]
        )
        side_of = {"只看话题/情境文本": "话题/情境", "只看互动线索文本": "互动线索"}
        best_side = side_of[best_half["name"]]
        worse_side = side_of[worse_half["name"]]
        spread = best_half["primary"] - worse_half["primary"]

        # Judged against the constant predictor, not against each other: with
        # thousands of evaluated cases a small gap between the halves is not
        # evidence, a gap to the baseline is.
        if best_half["primary"] <= reference["value"]:
            findings.append(
                {
                    "level": BAD,
                    "title": "拆开字段也救不了：两半都打不过常数预测器",
                    "detail": (
                        f"只留{best_side} {_p(best_half['primary'])}，"
                        f"只留{worse_side} {_p(worse_half['primary'])}，"
                        f"完整文本 {_p(baseline['value'])}，而"
                        f"{reference['label']}是 {_p(reference['value'])}。"
                        f"{best_side}确实比{worse_side}高 {_pt(spread)}，"
                        "但两半都远在基线之下。"
                    ),
                    "action": (
                        "瓶颈不在「用哪些字段匹配」，而在这些字段整体上预测不了策略。"
                        "不要在特征选择上继续投入，先处理标签层。"
                    ),
                }
            )
        elif best_half["primary"] > baseline["value"]:
            findings.append(
                {
                    "level": WARN,
                    "title": f"只用「{best_side}」反而比用全部字段更准",
                    "detail": (
                        f"只留{best_side} {_p(best_half['primary'])}，"
                        f"只留{worse_side} {_p(worse_half['primary'])}，"
                        f"完整文本只有 {_p(baseline['value'])}。"
                        f"也就是说，被丢掉的「{worse_side}」对策略预测是噪声。"
                    ),
                    "action": (
                        f"把「{worse_side}」从检索文本里摘出去，让打分只依赖"
                        f"「{best_side}」；这是一次改动能拿到收益、且不需要新数据的方向。"
                    ),
                }
            )
        else:
            findings.append(
                {
                    "level": INFO,
                    "title": f"「{best_side}」更能预测策略，但两半都没超过完整文本",
                    "detail": (
                        f"只留{best_side} {_p(best_half['primary'])}，"
                        f"只留{worse_side} {_p(worse_half['primary'])}，"
                        f"完整文本 {_p(baseline['value'])}。"
                        f"差距只有 {_pt(spread)}，在这个样本量上还不足以说明该丢字段。"
                    ),
                    "action": (
                        f"想沿着这个方向走，先把「{best_side}」拆成结构化字段"
                        "（谁在说、是不是在问、话题是新是旧），而不是当成又一段文本。"
                    ),
                }
            )

    # 6. Where is the remaining headroom? ---------------------------------- #
    if oracle.get("accuracy") is not None:
        gain = oracle["accuracy"] - reference["value"]
        if gain > 0.05:
            findings.append(
                {
                    "level": GOOD,
                    "title": "情境骨架还有空间，但它是上限不是现状",
                    "detail": (
                        f"若情境分类完全正确，图谱自身的情境→策略统计可达 "
                        f"{_p(oracle['accuracy'])}，比常数预测器高 {_pt(gain)}；"
                        f"当前只有 {oracle.get('families', 0)} 个情境族，"
                        f"而最优检索只有 {_p(best['value'])}。"
                    ),
                    "action": (
                        "这给「重做标签层」留出了明确的上限空间："
                        "先扩情境类型、拆开大策略，再评估检索能拿到多少。"
                    ),
                }
            )
        else:
            findings.append(
                {
                    "level": BAD,
                    "title": "情境层内部同样分不开策略",
                    "detail": (
                        f"即使情境分类完全正确，图谱自身的统计也只有 "
                        f"{_p(oracle['accuracy'])}，相对常数预测器只高 {_pt(gain)}。"
                        "瓶颈在「策略」这一层：同一情境下策略高度趋同。"
                    ),
                    "action": (
                        "先修标签体系而不是加特征：策略定义需要能区分开"
                        "「在什么情况下该做哪件事」，否则再好的特征也没有可学的目标。"
                    ),
                }
            )
    return findings


def _p(value: float | None) -> str:
    """A share, as a percentage."""
    return "—" if value is None else f"{value * 100:.1f}%"


def _pt(value: float | None) -> str:
    """A difference of two shares, as percentage *points*.

    "高 1.1%" reads as a relative gain and understates a 1.1-point lead on a
    35% base by roughly a factor of three.
    """
    return "—" if value is None else f"{value * 100:+.1f} 个百分点"
