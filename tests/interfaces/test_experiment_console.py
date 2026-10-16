"""Experiment lab: the console's read-only projection of the offline benchmark.

Two things are pinned here.

*The projection reads the report once, for both renderers.* The console section
and the standalone HTML page both lay out this projection, so a conclusion that
were derived in two places would eventually disagree with itself. These tests
assert on the projection rather than on either renderer.

*The majority baseline divides by the training side.* It is the share of the
biggest strategy *among the cases retrieval was allowed to see*. An earlier
version read the path count after the held-out paths had been put back, which
divided by the whole graph and quietly understated the bar — turning a
6.7-point shortfall into a 1.1-point one.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from qqbot.interfaces.webui.config import WebUIConfig
from qqbot.interfaces.webui.experiments import ExperimentConsoleQuery
from qqbot.interfaces.webui.server import WebUIServer

from tools.experiments.bench_behavior_retrieval import fold_of, run_holdout_strategy
from tests.agent.test_behavior_experience_runtime import write_graph


def variant(top1: float, *, any3: float = 0.5, coverage: float = 1.0, **extra) -> dict:
    row = {
        "cases": 100,
        "coverage_at_3": coverage,
        "top1_accuracy": top1,
        "any_at_3": any3,
        "top1_counts": {"supplement": 60, "clarify": 40},
        "per_class": {
            "supplement": {
                "support": 30,
                "share": 0.30,
                "recall_top1": 0.50,
                "recall_any3": 0.80,
                "predicted_share": 0.45,
                "precision_top1": 0.33,
                "lift": 0.03,
            },
            "clarify": {
                "support": 20,
                "share": 0.20,
                "recall_top1": 0.10,
                "recall_any3": 0.55,
                "predicted_share": 0.15,
                "precision_top1": 0.13,
                "lift": -0.07,
            },
        },
    }
    row.update(extra)
    return row


def sweep_point(weight: float, top1: float, *, fixed: int = 0, broke: int = 0) -> dict:
    return variant(
        top1,
        paired_vs_prior={
            "reference": "先验校准 λ=0（全猜先验）",
            "fixed": fixed,
            "broke": broke,
            "p_value": 0.12 if fixed and broke else 1.0,
        },
    )


def fold_row(fold: int, constant: float, best: float, **extra) -> dict:
    row = {
        "fold": fold,
        "held_out_cases": 40,
        "train_cases": 160,
        "majority": "supplement",
        "majority_on_holdout": constant,
        "variants": {
            "先验校准 λ=0（全猜先验）": constant,
            "先验校准 λ=1.5": best,
        },
    }
    row.update(extra)
    return row


def report(**holdout_overrides) -> dict:
    """A miniature of what the benchmark writes, with the same field names."""
    holdout = {
        "protocol": "holdout_strategy",
        "description": "按会话 3 折交叉验证",
        "folds": 3,
        "held_out_sessions": 30,
        "held_out_cases": 120,
        "graph_cases": 120,
        "train_cases": 80,
        "majority_class": "supplement",
        "majority_baseline_top1": 0.40,
        "majority_baseline_on_holdout": 0.30,
        "held_out_strategy_distribution": {"supplement": 36, "clarify": 84},
        "train_strategy_distribution": {"supplement": 64, "clarify": 96},
        "taxonomy": {
            "fine_classes": 13,
            "coarse_classes": 9,
            "mapping": {"supplement": "附和", "banter": "玩梗"},
        },
        "situation_oracle": {
            "description": "若情境分类完全正确",
            "families": 9,
            "accuracy": 0.42,
            "per_family_top_strategy": {"direct_question": {"answer_directly": 5}},
        },
        "per_fold": [
            fold_row(0, 0.30, 0.35),
            fold_row(1, 0.32, 0.30),
            fold_row(2, 0.28, 0.33),
        ],
        "variants": {
            "baseline（现状）": variant(
                0.25,
                any3=0.56,
                paired_vs_prior={
                    "reference": "先验校准 λ=0（全猜先验）",
                    "fixed": 90,
                    "broke": 200,
                    "p_value": 0.0001,
                },
            ),
            "只看话题/情境文本": variant(0.21, any3=0.59),
            "只看互动线索文本": variant(0.26, any3=0.59),
            "IDF 加权（无任何加分）": variant(0.29, any3=0.61),
            "一个还没被写进分组表的变体": variant(0.20),
            "先验校准 λ=0（全猜先验）": sweep_point(0.0, 0.30),
            "先验校准 λ=3": sweep_point(3.0, 0.33, fixed=22, broke=12),
            "先验校准 λ=10": sweep_point(10.0, 0.28, fixed=28, broke=34),
        },
    }
    holdout.update(holdout_overrides)
    legacy = {
        "protocol": "legacy_family",
        "description": "原代理指标",
        "variants": {
            "baseline（现状）": variant(
                0.0,
                family_top1=0.27,
                majority_family="general_group_exchange",
                majority_family_baseline=0.61,
            ),
        },
    }
    return {
        "schema_version": 1,
        "graph": "data/private/derived/graph",
        "corpus": "data/private/derived/corpus",
        "graph_cases": 200,
        "protocols": [holdout, legacy],
    }


def write_report(tmp_path: Path, body: dict) -> Path:
    path = tmp_path / "behavior-retrieval.json"
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    return path


def view_of(tmp_path: Path, body: dict) -> dict:
    return ExperimentConsoleQuery(write_report(tmp_path, body)).behavior_retrieval()


def rows_of(protocol: dict) -> list[dict]:
    return [row for group in protocol["groups"] for row in group["rows"]]


# ------------------------------------------------------------------ projection


def test_the_projection_carries_the_label_system(tmp_path: Path) -> None:
    """The report must say which label system it measured.

    The benchmark defaults to the shipped taxonomy, but it can also be run with
    ``--taxonomy none`` to reproduce the raw 13-class numbers. If the projection
    dropped that field, the console would happily show 13-class numbers under a
    9-class heading and nobody would notice.
    """
    view = view_of(tmp_path, report())
    assert view["protocols"][0]["taxonomy"]["coarse_classes"] == 9
    assert view["protocols"][0]["taxonomy"]["fine_classes"] == 13
    assert view["headline"]["taxonomy"]["mapping"]["supplement"] == "附和"


def test_a_report_without_a_taxonomy_reads_as_raw_labels(tmp_path: Path) -> None:
    view = view_of(tmp_path, report(taxonomy=None))
    assert view["protocols"][0]["taxonomy"] is None
    assert view["headline"]["taxonomy"] is None


def test_a_missing_report_is_an_empty_state_not_an_error(tmp_path: Path) -> None:
    view = ExperimentConsoleQuery(tmp_path / "nope.json").behavior_retrieval()
    assert view["available"] is False
    assert "还没有实验数据" in view["reason"]
    # The empty state has to say how to produce the data, or it is a dead end.
    assert view["command"].endswith("tools.experiments.bench_behavior_retrieval")


def test_a_corrupt_report_is_reported_rather_than_raised(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")
    view = ExperimentConsoleQuery(path).behavior_retrieval()
    assert view["available"] is False
    assert "JSONDecodeError" in view["detail"]


def test_the_reference_is_what_the_constant_predictor_scores_on_holdout(
    tmp_path: Path,
) -> None:
    """Not the training share it is derived from — those differ, and using the
    training share as the bar misstates the bar by several points."""
    view = view_of(tmp_path, report())
    primary = view["protocols"][0]
    assert primary["reference"]["value"] == 0.30
    assert primary["reference"]["train_value"] == 0.40
    assert "supplement" in primary["reference"]["label"]
    assert "训练侧占 40.0%" in primary["reference"]["note"]


def test_the_best_variant_is_the_highest_primary_metric(tmp_path: Path) -> None:
    view = view_of(tmp_path, report())
    assert view["headline"]["best_name"] == "先验校准 λ=3"
    assert view["headline"]["best_value"] == 0.33
    assert view["headline"]["best_delta"] == pytest.approx(0.08)
    assert view["headline"]["beats_reference"] is True


def test_a_gain_that_does_not_clear_significance_is_reported_as_such(
    tmp_path: Path,
) -> None:
    """A 3-point win with p=0.12 is not a win. The verdict must say so rather
    than colour the number green."""
    view = view_of(tmp_path, report())
    assert view["headline"]["best_significant"] is False
    finding = next(item for item in view["findings"] if "统计显著" in item["title"])
    assert finding["level"] == "warn"
    assert "p=0.120" in finding["detail"]
    assert "增加评测样本" in finding["action"]


def test_a_significant_gain_is_reported_as_a_real_win(tmp_path: Path) -> None:
    body = report()
    body["protocols"][0]["variants"]["先验校准 λ=3"] = variant(
        0.33,
        paired_vs_prior={
            "reference": "先验校准 λ=0（全猜先验）",
            "fixed": 30,
            "broke": 5,
            "p_value": 0.001,
        },
    )
    view = view_of(tmp_path, body)
    assert view["headline"]["best_significant"] is True
    finding = next(
        item for item in view["findings"] if "超过了「永远回答同一个策略」" in item["title"]
    )
    assert finding["level"] == "good"
    assert "p=0.001" in finding["detail"]


def test_the_cost_of_not_calibrating_is_reported_as_its_own_finding(
    tmp_path: Path,
) -> None:
    """Every un-calibrated variant scores below doing nothing, and the paired
    test calls it decisive. That is the single most actionable fact in the
    report, so it gets its own claim rather than being buried in the table."""
    view = view_of(tmp_path, report())
    finding = next(item for item in view["findings"] if "不校准" in item["title"])
    assert finding["level"] == "warn"
    assert "现状打分是 25.0%" in finding["detail"]
    assert "+8.0 个百分点" in finding["action"]


def test_a_gain_is_reported_in_percentage_points_not_percent(tmp_path: Path) -> None:
    """A 1.1-point lead on a 35% base is a 3% relative gain. Writing it as
    "高 1.1%" makes a real effect look negligible."""
    view = view_of(tmp_path, report())
    finding = next(item for item in view["findings"] if "不校准" in item["title"])
    assert "+3.0 个百分点" in finding["detail"]


def test_fold_disagreement_is_surfaced(tmp_path: Path) -> None:
    """A pooled number hides an unstable result: one good fold and two bad ones
    averages to the same figure as a consistent win."""
    view = view_of(tmp_path, report())
    per_fold = view["protocols"][0]["per_fold"]
    assert [row["fold"] for row in per_fold] == [0, 1, 2]
    assert [row["beats_constant"] for row in per_fold] == [True, False, True]
    finding = next(item for item in view["findings"] if "折里" in item["title"])
    assert finding["level"] == "warn"
    assert "2 折方向一致" in finding["title"]


def test_the_sweep_series_is_extracted_in_weight_order(tmp_path: Path) -> None:
    view = view_of(tmp_path, report())
    sweep = view["protocols"][0]["sweep"]
    assert [point["weight"] for point in sweep] == [0.0, 3.0, 10.0]
    assert sweep[0]["primary"] == 0.30
    assert sweep[1]["paired"]["fixed"] == 22
    assert sweep[1]["paired"]["significant"] is False
    # The peak is the whole point of the curve, and it is not the last point.
    assert max(sweep, key=lambda point: point["primary"])["weight"] == 3.0


def test_unlisted_variants_still_render(tmp_path: Path) -> None:
    """A new experiment must not vanish because the group table is stale."""
    body = report()
    view = view_of(tmp_path, body)
    names = [row["name"] for row in rows_of(view["protocols"][0])]
    assert "一个还没被写进分组表的变体" in names
    assert len(names) == len(body["protocols"][0]["variants"])
    assert any(group["title"] == "其他" for group in view["protocols"][0]["groups"])


def test_two_halves_both_losing_to_the_baseline_blames_the_fields(
    tmp_path: Path,
) -> None:
    """Neither half clearing the bar means field selection is not the problem,
    so the projection must not recommend dropping a field."""
    view = view_of(tmp_path, report())
    finding = next(item for item in view["findings"] if "两半都打不过" in item["title"])
    assert finding["level"] == "bad"
    assert "26.0%" in finding["detail"] and "21.0%" in finding["detail"]
    assert "先处理标签层" in finding["action"]


def test_a_half_that_beats_the_full_text_is_reported_as_dilution(
    tmp_path: Path,
) -> None:
    """Once the half clears the bar, the comparison against the *full* index
    becomes meaningful: a half beating the whole means the other half hurts."""
    body = report(majority_baseline_on_holdout=0.20)
    view = view_of(tmp_path, body)
    finding = next(item for item in view["findings"] if "更准" in item["title"])
    assert finding["level"] == "warn"
    assert "互动线索" in finding["title"]
    assert "摘出去" in finding["action"]


def test_a_half_that_clears_the_bar_without_beating_the_whole_is_a_weak_signal(
    tmp_path: Path,
) -> None:
    """Clearing the bar but not the full text is not enough to justify dropping
    a field on a few hundred cases."""
    body = report(majority_baseline_on_holdout=0.20)
    body["protocols"][0]["variants"]["只看话题/情境文本"] = variant(0.20)
    body["protocols"][0]["variants"]["只看互动线索文本"] = variant(0.22)
    view = view_of(tmp_path, body)
    finding = next(item for item in view["findings"] if "没超过完整文本" in item["title"])
    assert finding["level"] == "info"
    assert "不足以说明该丢字段" in finding["detail"]
    assert "摘出去" not in finding["action"]


def test_the_legacy_protocol_scores_a_different_metric(tmp_path: Path) -> None:
    """``top1_accuracy`` is structurally zero there — the truth is a family, so
    comparing it to a strategy string can never match. Reading it anyway would
    show 0% for every variant and say nothing."""
    view = view_of(tmp_path, report())
    legacy = view["protocols"][1]
    assert legacy["id"] == "legacy_family"
    assert legacy["metric"]["primary_label"] == "情境族命中"
    assert legacy["metric"]["show_coverage"] is False
    assert legacy["best"]["value"] == 0.27
    # The real comparison is against the keyword table's fallback bucket.
    assert legacy["reference"]["value"] == 0.61


def test_findings_are_empty_without_a_holdout_protocol(tmp_path: Path) -> None:
    body = report()
    body["protocols"] = [body["protocols"][1]]
    view = view_of(tmp_path, body)
    assert view["findings"] == []
    assert view["headline"] == {}


def test_a_situation_ceiling_close_to_the_baseline_blames_the_label_layer(
    tmp_path: Path,
) -> None:
    """When a perfect situation classifier would still only match the constant
    predictor, the fix is the taxonomy, not the features."""
    view = view_of(
        tmp_path,
        report(
            majority_baseline_on_holdout=0.40,
            situation_oracle={
                "description": "若情境分类完全正确",
                "families": 9,
                "accuracy": 0.41,
                "per_family_top_strategy": {},
                "held_out_family_distribution": {},
            },
        ),
    )
    finding = next(item for item in view["findings"] if "情境层" in item["title"])
    assert finding["level"] == "bad"
    assert "标签体系" in finding["action"]


# ----------------------------------------------------------------- the route


def _request(url: str, token: str = ""):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with urllib.request.urlopen(
        urllib.request.Request(url, headers=headers), timeout=2
    ) as response:
        return response.status, json.loads(response.read())


def test_the_experiment_endpoint_is_owner_only_and_serves_the_report(
    tmp_path: Path,
) -> None:
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    server = WebUIServer(WebUIConfig(True, "127.0.0.1", 0, "test-token", env))
    server.experiments = ExperimentConsoleQuery(write_report(tmp_path, report()))
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_port}"
    try:
        with pytest.raises(urllib.error.HTTPError) as denied:
            _request(base + "/api/experiments/behavior-retrieval")
        assert denied.value.code == 401
        denied.value.close()

        status, body = _request(
            base + "/api/experiments/behavior-retrieval", "test-token"
        )
        assert status == 200
        assert body["data"]["available"] is True
        assert body["data"]["headline"]["best_value"] == 0.33
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_the_experiment_endpoint_survives_a_console_with_no_report(
    tmp_path: Path,
) -> None:
    """The section is file-backed, so it must not 500 just because the operator
    has never run the benchmark."""
    env = tmp_path / ".env"
    env.write_text("", encoding="utf-8")
    server = WebUIServer(WebUIConfig(True, "127.0.0.1", 0, "test-token", env))
    server.experiments = ExperimentConsoleQuery(tmp_path / "absent.json")
    from http.server import ThreadingHTTPServer

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server._handler())
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _request(
            f"http://127.0.0.1:{httpd.server_port}/api/experiments/behavior-retrieval",
            "test-token",
        )
        assert status == 200
        assert body["data"]["available"] is False
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


# ------------------------------------------------------- the benchmark's maths


def session_ids(fold: int, folds: int, count: int) -> list[str]:
    """Deterministic session ids that hash into ``fold``."""
    found, index = [], 0
    while len(found) < count:
        candidate = f"s{fold}-{index}"
        if fold_of(candidate, folds) == fold:
            found.append(candidate)
        index += 1
    return found


FOLDS = 3


def bench_fixture(tmp_path: Path):
    """A six-case graph spread across three session folds, two cases each."""
    plan = [
        ("answer_directly", "direct_question"),
        ("answer_directly", "direct_question"),
        ("supplement", "general_group_exchange"),
        ("supplement", "general_group_exchange"),
        ("supplement", "general_group_exchange"),
        ("banter", "banter_or_joke"),
    ]
    cases = [
        {
            "situation": f"情境 {index}",
            "topics": [f"话题{index}"],
            "strategy": strategy,
            "act": "reply",
            "expression": f"表达 {index}",
            "variants": [f"说法 {index}"],
            "family": family,
            "support": 1,
        }
        for index, (strategy, family) in enumerate(plan)
    ]
    root = write_graph(tmp_path / "graph", cases)

    # Two cases per fold, so every fold holds out a session and trains on four.
    sessions = [
        session
        for fold in range(FOLDS)
        for session in session_ids(fold, FOLDS, 1)
        for _ in range(2)
    ]
    messages, corpus_cases = {}, {}
    for index in range(len(plan)):
        message_id = f"m{index}"
        messages[message_id] = {"text": f"消息内容 {index}"}
        corpus_cases[f"c{index}"] = {
            "id": f"c{index}",
            "session_id": sessions[index],
            "split": "test",
            "context_ids": [message_id],
        }

    from qqbot.agent.cognition.experience.graph_retrieval import BehaviorGraphRetriever

    return BehaviorGraphRetriever(root), messages, corpus_cases


def test_every_case_is_evaluated_once_while_the_training_side_still_shrinks(
    tmp_path: Path,
) -> None:
    """Cross-validation over sessions is the whole point of the protocol: a
    single holdout leaves too few cases for a paired test to decide anything,
    and every case can be tested once without giving the retriever more."""
    retriever, messages, corpus_cases = bench_fixture(tmp_path)
    graph_cases = len(retriever.case_paths)

    result = run_holdout_strategy(retriever, messages, corpus_cases, FOLDS)

    assert result["folds"] == FOLDS
    assert result["graph_cases"] == graph_cases
    assert len(result["per_fold"]) == FOLDS
    assert result["held_out_cases"] == graph_cases
    assert sum(fold["held_out_cases"] for fold in result["per_fold"]) == graph_cases
    # Each fold still trains on a proper subset — that is what keeps it honest.
    assert result["train_cases"] < graph_cases
    assert all(fold["train_cases"] < graph_cases for fold in result["per_fold"])


def test_the_constant_predictor_is_scored_on_the_evaluated_cases(
    tmp_path: Path,
) -> None:
    """The prior is estimated per fold on the training side but judged on the
    evaluated cases, and the two distributions differ. Dividing by the wrong
    one misstates the bar the retrieval has to clear."""
    retriever, messages, corpus_cases = bench_fixture(tmp_path)

    result = run_holdout_strategy(retriever, messages, corpus_cases, FOLDS)

    weighted = sum(
        fold["majority_on_holdout"] * fold["held_out_cases"]
        for fold in result["per_fold"]
    )
    assert result["majority_baseline_on_holdout"] == pytest.approx(
        weighted / result["held_out_cases"], abs=1e-3
    )
    # And the pure-prior variant must reproduce it exactly: lambda = 0 is the
    # constant predictor by construction, which makes it the sweep's own check
    # that the blending is wired up.
    pure = result["variants"]["先验校准 λ=0（全猜先验）"]
    assert pure["top1_accuracy"] == pytest.approx(
        result["majority_baseline_on_holdout"], abs=1e-4
    )


def test_the_benchmark_restores_the_graph_after_the_holdout(tmp_path: Path) -> None:
    """The protocol mutates ``case_paths`` to build each training side; leaving
    it reduced would silently shrink the graph for every later protocol."""
    retriever, messages, corpus_cases = bench_fixture(tmp_path)
    before = list(retriever.case_paths)

    run_holdout_strategy(retriever, messages, corpus_cases, FOLDS)

    assert retriever.case_paths == before
    # Every posting index must point back inside the restored graph, which is
    # only true if the index was rebuilt over the full path list.
    assert max(max(rows) for rows in retriever._postings.values()) == len(before) - 1
