"""Render the behavior-retrieval benchmark into a self-contained HTML page.

This is the offline twin of the console's "实验看板" section. Both read the same
projection (:mod:`qqbot.interfaces.webui.experiments`), so the numbers and the
conclusions cannot disagree between the two — only the layout differs.

The page inlines its data instead of fetching it: a file opened straight from
disk cannot ``fetch()`` a sibling JSON, and being double-clickable is the point.

    .venv/bin/python -m tools.experiments.bench_behavior_retrieval
    .venv/bin/python -m tools.experiments.render_behavior_dashboard
"""

from __future__ import annotations

import argparse
import html
import sys
from datetime import datetime
from pathlib import Path


from qqbot.interfaces.webui.experiments import ExperimentConsoleQuery

DEFAULT_OUTPUT = Path("experiments/behavior-retrieval-dashboard.html")

GOOD, BAD, WARN, INFO = "#0F6E56", "#A32D2D", "#8A6519", "#185FA5"
NEUTRAL = "#5F5E5A"

LEVEL_COLOR = {"good": GOOD, "bad": BAD, "warn": WARN, "info": INFO}
LEVEL_LABEL = {"good": "利好", "warn": "注意", "bad": "问题", "info": "观察"}
BAR_COLOR = {"better": "#2F9E77", "worse": "#D0607A", "same": "#93A3B5"}


def e(value) -> str:
    return html.escape(str(value if value is not None else ""))


def percent(value, digits: int = 1) -> str:
    return "—" if value is None else f"{value * 100:.{digits}f}%"


def signed(value, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value * 100:+.{digits}f}" if value else f"{value * 100:.{digits}f}"


def bar(value: float, maximum: float, level: str) -> str:
    width = 0.0 if maximum <= 0 else max(0.0, min(1.0, value / maximum)) * 100
    return (
        f'<div class="bar"><span style="width:{width:.1f}%;'
        f'background:{BAR_COLOR.get(level, BAR_COLOR["same"])}"></span></div>'
    )


# --------------------------------------------------------------------------- #
# fragments
# --------------------------------------------------------------------------- #


def headline_block(view: dict) -> str:
    head = view.get("headline") or {}
    oracle = head.get("oracle_value")
    cards = [
        ("图谱案例", view.get("graph_cases") or "—", "编译后的行为图谱规模"),
        ("留出案例", head.get("held_out_cases") or "—", f"{head.get('held_out_sessions') or 0} 个整会话未参与检索"),
        (head.get("reference_label") or "多数类基线", percent(head.get("reference_value")), "任何变体都要先超过它"),
        ("最优变体", percent(head.get("best_value")), head.get("best_name") or ""),
    ]
    verdict_class = "good" if head.get("beats_reference") and head.get("best_significant") else ("warn" if head.get("beats_reference") else "bad")
    paired = head.get("best_paired") or {}
    over_reference = (head.get("best_value") or 0) - (head.get("reference_value") or 0)
    if head.get("beats_reference") and head.get("best_significant"):
        verdict = f"最优变体显著超过常数预测器 {signed(over_reference)}（p={paired.get('p_value', 1):.3f}）"
    elif head.get("beats_reference"):
        verdict = (
            f"最优变体高于常数预测器 {signed(over_reference)}，"
            f"但配对检验 p={paired.get('p_value', 1):.3f}，未达显著"
        )
    else:
        verdict = f"最优变体仍低于常数预测器，差 {signed(over_reference)}"
    oracle_chip = (
        ""
        if oracle is None
        else f'<span class="chip info">情境完美时的上限 {percent(oracle)}'
        f'（{head.get("oracle_families") or 0} 个情境族）</span>'
    )
    tax = head.get("taxonomy") or {}
    taxonomy_chip = (
        f'<span class="chip info">标签体系 {tax.get("coarse_classes")} 类'
        f'（由 {tax.get("fine_classes")} 个细类合并，与线上代码一致）</span>'
        if tax
        else '<span class="chip">标签体系 原始细类</span>'
    )
    return f"""
  <div class="cards">
    {"".join(f'<div class="card"><span>{e(label)}</span><b>{e(value)}</b><small>{e(note)}</small></div>' for label, value, note in cards)}
  </div>
  <div class="verdict-row">
    <span class="chip {verdict_class}">{e(verdict)}</span>
    {oracle_chip}
    {taxonomy_chip}
  </div>"""


def findings_block(view: dict) -> str:
    findings = view.get("findings") or []
    if not findings:
        return ""
    items = "".join(
        f"""
    <article class="finding {e(item['level'])}">
      <div class="finding-head"><span class="level">{e(LEVEL_LABEL.get(item['level'], '观察'))}</span><h4>{e(item['title'])}</h4></div>
      <p>{e(item['detail'])}</p>
      <div class="action"><b>下一步</b><span>{e(item['action'])}</span></div>
    </article>"""
        for item in findings
    )
    return f"""
  <section class="block">
    <div class="block-head"><h3>结论与下一步</h3><span class="muted">由本次实验数据推出，重新跑实验后自动更新</span></div>
    {items}
  </section>"""


def variants_table(protocol: dict) -> str:
    metric = protocol.get("metric") or {}
    rows = []
    for group in protocol.get("groups") or []:
        rows.append(
            f'<tr class="group"><th colspan="7">{e(group["title"])}'
            f'<span class="blurb">{e(group["blurb"])}</span></th></tr>'
        )
        for row in group["rows"]:
            marker = "基准" if row["is_baseline"] else {"better": "↑", "worse": "↓"}.get(row["level"], "=")
            coverage = (
                f'<td class="num">{percent(row["coverage"])}</td>'
                if metric.get("show_coverage")
                else ""
            )
            secondary = (
                f'<td class="num">{percent(row["secondary"])}</td>'
                if metric.get("secondary_label")
                else ""
            )
            rows.append(
                f"""
      <tr class="{'is-baseline' if row['is_baseline'] else ''}">
        <td class="name">{e(row['name'])}</td>
        {coverage}{secondary}
        <td class="num strong">{percent(row['primary'])}</td>
        <td class="num delta {e(row['level'])}">{marker} <span class="mono">{'—' if row['is_baseline'] else signed(row['delta'])}</span></td>
        <td class="num">{paired_cell(row.get('paired'))}</td>
        <td class="graph">{bar(row['primary'], protocol['maximum'], row['level'])}</td>
      </tr>"""
            )
    reference = protocol.get("reference") or {}
    train_note = (
        f"（训练侧占 {percent(reference['train_value'])}）"
        if reference.get("train_value") is not None
        else ""
    )
    return f"""
    <div class="reference">
      <div><span>必须超过的基线</span><b>{percent(reference.get('value'))}</b><small>{e(reference.get('label'))}{train_note}</small></div>
      <p>{e(reference.get('note'))}</p>
    </div>
    <table>
      <thead><tr>
        <th>评分变体</th>
        {'<th class="num">覆盖@3</th>' if metric.get('show_coverage') else ''}
        {f'<th class="num">{e(metric["secondary_label"])}</th>' if metric.get('secondary_label') else ''}
        <th class="num">{e(metric.get('primary_label'))}</th>
        <th class="num">对比现状</th>
        <th class="num">配对检验 vs 纯先验</th>
        <th class="graph">分布</th>
      </tr></thead>
      <tbody>{"".join(rows)}</tbody>
    </table>
    <p class="foot">「{e(metric.get('primary_label'))}」：{e(metric.get('primary_help'))}{f"；「{e(metric['secondary_label'])}」：{e(metric['secondary_help'])}" if metric.get('secondary_label') else ''}。
    「配对检验」只看两个排序在同一批案例上不一致的那些：修好的条数明显多于弄坏的、且 p &lt; 0.05，才算真的赢。</p>"""


def paired_cell(paired: dict | None) -> str:
    if not paired:
        return "—"
    colour = GOOD if paired["significant"] else (WARN if paired["fixed"] > paired["broke"] else NEUTRAL)
    return (
        f'<span class="mono" style="color:{colour}">'
        f'{paired["fixed"]}↑ / {paired["broke"]}↓ p={paired["p_value"]:.3f}'
        f'</span>'
    )


def sweep_block(protocol: dict) -> str:
    """The calibration curve: does any weight beat the constant predictor?

    Drawn as a table rather than a chart because the interesting information is
    the paired counts and the p-value, not the shape.
    """
    sweep = protocol.get("sweep") or []
    if len(sweep) < 2:
        return ""
    peak = max(sweep, key=lambda row: row["primary"])
    pure = next((row for row in sweep if row["weight"] == 0), None)
    rows = "".join(
        f"""
      <tr class="{'is-baseline' if row is peak else ''}">
        <td class="name mono">λ={row['weight']:g}</td>
        <td class="num strong">{percent(row['primary'])}</td>
        <td class="num">{percent(row['secondary'])}</td>
        <td class="num">{paired_cell(row.get('paired'))}</td>
        <td class="graph">{bar(row['primary'], protocol['maximum'], 'better' if row is peak else 'same')}</td>
      </tr>"""
        for row in sweep
    )
    gain = peak["primary"] - (pure["primary"] if pure else 0.0)
    return f"""
  <section class="block">
    <div class="block-head"><h3>先验校准曲线</h3><span class="muted">λ=0 是构造出来的常数预测器，是这条曲线的起点</span></div>
    <table>
      <thead><tr><th>权重</th><th class="num">Top-1 命中</th><th class="num">任意@3</th><th class="num">配对检验 vs λ=0</th><th class="graph">分布</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p class="foot">峰值在 λ={peak['weight']:g}，{percent(peak['primary'])}，比纯先验高 {percent(gain)}。
    λ 越大越像纯检索；曲线两端都低、中间有峰，说明检索和先验各自都缺一半信息。</p>
  </section>"""


def per_class_block(protocol: dict) -> str:
    table = protocol.get("per_class") or {}
    rows_in = table.get("rows") or []
    if not rows_in:
        return ""
    best = {row["strategy"]: row for row in (protocol.get("per_class_best") or {}).get("rows", [])}
    rows = "".join(
        f"""
      <tr>
        <td class="name">{e(row['strategy'])}</td>
        <td class="num">{percent(row['share'])}</td>
        <td class="num strong">{percent(row['recall_top1'])}</td>
        <td class="num">{percent(row['precision_top1'])}</td>
        <td class="num" style="color:{GOOD if row.get('lift', 0) > 0.02 else (BAD if row.get('lift', 0) < 0 else NEUTRAL)}">{signed(row.get('lift', 0))}</td>
        <td class="num">{percent(row['recall_any3'])}</td>
        <td class="num">{percent(best.get(row['strategy'], {}).get('recall_top1'))}</td>
        <td class="graph">{bar(row['precision_top1'], 1.0, 'better' if row.get('lift', 0) > 0 else 'worse')}</td>
      </tr>"""
        for row in rows_in
    )
    return f"""
  <section class="block">
    <div class="block-head"><h3>分类召回与精确率</h3><span class="muted">聚合准确率会掩盖失败模式，逐类看才看得出检索到底认出了什么</span></div>
    <table>
      <thead><tr>
        <th>策略</th><th class="num">占比</th><th class="num">Top-1 召回</th>
        <th class="num">精确率</th><th class="num">提升</th>
        <th class="num">任意@3 召回</th><th class="num">最优变体召回</th><th class="graph">精确率 vs 占比</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p class="foot">基准变体：{e(table.get('variant'))}；最优变体：{e((protocol.get('per_class_best') or {}).get('variant'))}。
    <b>召回</b>高于占比说明这一类被认出来了；<b>精确率</b>高于占比（即「提升」为正）说明「预测成这一类」本身带信息，而不是靠先验凑数。
    提升为负的类等于从未被正确预测过。</p>
  </section>"""


def shift_block(protocol: dict) -> str:
    shift = protocol.get("prediction_shift") or {}
    rows_in = shift.get("rows") or []
    if not rows_in:
        return ""
    rows = "".join(
        f"""
      <tr>
        <td class="name">{e(row['strategy'])}</td>
        <td class="num">{percent(row['prior'])}</td>
        <td class="num">{percent(row['baseline'])}</td>
        <td class="num">{percent(row['best'])}</td>
        <td class="graph">{bar(row['best'], 1.0, 'worse' if row['best'] > row['prior'] + 0.05 else 'same')}</td>
      </tr>"""
        for row in rows_in
    )
    return f"""
  <section class="block">
    <div class="block-head"><h3>预测分布 vs 真实占比</h3><span class="muted">用来判断准确率的上涨是不是只靠多猜最大类</span></div>
    <table>
      <thead><tr><th>策略</th><th class="num">训练侧占比</th><th class="num">基准预测</th><th class="num">最优预测</th><th class="graph">偏移</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p class="foot">「训练侧占比」是这一类真实出现的比例。预测比例明显高于它，说明系统在往先验上塌。</p>
  </section>"""


def folds_block(protocol: dict) -> str:
    """Per-fold accuracy, because a pooled number hides an unstable result.

    One good fold and four bad ones averages to the same figure as a consistent
    win, and only the per-fold rows tell them apart.
    """
    folds = protocol.get("per_fold") or []
    if len(folds) < 2:
        return ""
    rows = "".join(
        f"""
      <tr>
        <td class="name">折 {fold['fold']}</td>
        <td class="num">{fold['held_out_cases']}</td>
        <td class="num">{fold['train_cases']}</td>
        <td class="num">{percent(fold['constant'])}</td>
        <td class="num strong">{percent(fold['best'])}</td>
        <td class="num" style="color:{GOOD if fold['beats_constant'] else BAD}">{signed(fold.get('edge'))}</td>
        <td class="name mono">{e(fold['best_name'])}</td>
      </tr>"""
        for fold in folds
    )
    agree = sum(1 for fold in folds if fold["beats_constant"])
    return f"""
  <section class="block">
    <div class="block-head"><h3>每折结果</h3><span class="muted">{len(folds)} 折里 {agree} 折的最优变体高于该折的常数预测器</span></div>
    <table>
      <thead><tr>
        <th>折</th><th class="num">评测条数</th><th class="num">训练条数</th>
        <th class="num">该折常数预测器</th><th class="num">该折最优</th><th class="num">差距</th><th>最优变体</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p class="foot">折之间训练集互不相同，但评测覆盖全部案例且互不重叠。方向一致才说明结论不是划分方式带来的偶然。</p>
  </section>"""


def protocol_block(protocol: dict) -> str:
    counts = protocol.get("counts") or {}
    split = ""
    if protocol["id"] == "holdout_strategy" and counts.get("folds"):
        split = (
            f'<div class="split"><span>{counts["folds"]} 折交叉验证</span>'
            f'<span>每折训练约 {counts.get("train_cases")} 条</span>'
            f'<span>共评测 {counts.get("held_out_cases")} 条 / {counts.get("held_out_sessions")} 个会话</span></div>'
        )
    return f"""
  <section class="protocol">
    <div class="protocol-head">
      <div>
        <p class="eyebrow">{e('PRIMARY PROTOCOL' if protocol['id'] == 'holdout_strategy' else 'LEGACY REPRODUCTION')}</p>
        <h3>{e(protocol['title'])}</h3>
        <p class="muted">{e(protocol['description'])}</p>
      </div>
      <span class="pill">{e(protocol['headline'])}</span>
    </div>
    {split}
    {variants_table(protocol)}
    {sweep_block(protocol) if protocol['id'] == 'holdout_strategy' else ''}
    {folds_block(protocol) if protocol['id'] == 'holdout_strategy' else ''}
    {per_class_block(protocol) if protocol['id'] == 'holdout_strategy' else ''}
    {shift_block(protocol) if protocol['id'] == 'holdout_strategy' else ''}
  </section>"""


def method_block(view: dict) -> str:
    head = view.get("headline") or {}
    legacy = next((p for p in view.get("protocols", []) if p["id"] == "legacy_family"), None)
    legacy_floor = percent((legacy or {}).get("reference", {}).get("value"))
    oracle = head.get("oracle_value")
    items = [
        "<b>只看第一个协议。</b>第二个协议复现的是旧指标：它的期望标签来自关键词表，兜底桶占了 "
        f"{legacy_floor} 以上，本身不是正确性标准。",
        f"<b>先看多数类基线。</b>{e(head.get('reference_label'))}是 {percent(head.get('reference_value'))}。"
        "如果所有变体都没超过它，说明检索还不如「永远回答同一个策略」，调参没有意义。",
        "<b>再看分类召回。</b>聚合准确率会掩盖失败模式：一个只会预测最大类的系统能拿到与先验持平的分数，却在其它类上完全无用。",
        "<b>最后看字段拆分。</b>「只看话题/情境文本」和「只看互动线索文本」这两行直接回答「检索靠哪半边在匹配」，"
        "这是决定优化方向的关键一组数字。",
        f"<b>情境上限。</b>「若情境分类完全正确」是图谱自身情境→策略统计能达到的天花板 {percent(oracle)}。"
        "它衡量的是分类体系给得起多少空间，不是当前能拿到多少。",
        f"<b>数据来自文件。</b>本页读的是 <span class=\"mono\">{e(view.get('path'))}</span>，"
        f"由 <span class=\"mono\">{e('.venv/bin/python -m tools.experiments.bench_behavior_retrieval')}</span> 生成。",
    ]
    return f"""
  <section class="protocol">
    <div class="protocol-head"><div><p class="eyebrow">HOW TO READ</p><h3>指标说明</h3><p class="muted">这张看板为什么这样设计</p></div></div>
    <ol class="method">{"".join(f"<li>{item}</li>" for item in items)}</ol>
  </section>"""


STYLE = """
:root{--bg:#F2F7FD;--card:#fff;--line:#DCE7F4;--ink:#14233B;--muted:#66768D;--accent:#1769E0}
*{box-sizing:border-box}
body{margin:0;padding:40px 24px 80px;background:radial-gradient(circle at 88% 4%,#D9EAFF 0,transparent 29%),var(--bg);color:var(--ink);
  font-family:Inter,"PingFang SC","Microsoft YaHei",system-ui,sans-serif;font-size:14px;line-height:1.65}
.wrap{max-width:1060px;margin:0 auto}
h1{margin:0 0 6px;font:700 24px/1.25 Georgia,"Songti SC",serif}
h3{margin:0;font-size:16px;color:#183A62}
h4{margin:0;font-size:13px;color:#183A62}
.eyebrow{margin:0 0 6px;color:var(--accent);font-size:10px;font-weight:800;letter-spacing:.16em}
.muted{color:var(--muted);margin:0;font-size:11px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px}
header{border-bottom:1px solid var(--line);padding-bottom:20px;margin-bottom:24px}
header .source{margin-top:10px;color:var(--muted);font-size:10px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px}
.card{padding:16px 18px;border:1px solid var(--line);border-radius:14px;background:linear-gradient(145deg,#fff,#F5F9FE)}
.card span,.card small{display:block;color:var(--muted);font-size:10px}
.card b{display:block;margin:8px 0 6px;color:#153B68;font:700 26px/1 Georgia,serif}
.verdict-row{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:26px}
.chip{padding:7px 12px;border-radius:999px;font-size:11px;font-weight:800}
.chip.good{color:#167057;background:#E7F7F1}.chip.bad{color:#9B3750;background:#FDEBF0}.chip.info{color:#315E91;background:#E7F1FD}.chip.warn{color:#8A6519;background:#FFF4D9}
.block{margin-bottom:26px}
.block-head{display:flex;align-items:baseline;justify-content:space-between;gap:16px;margin-bottom:13px}
.finding{padding:15px 17px;border:1px solid var(--line);border-left:3px solid #ccc;border-radius:12px;background:#fff}
.finding+.finding{margin-top:10px}
.finding.good{border-left-color:#2F9E77;background:linear-gradient(90deg,#F4FCF9,#fff 42%)}
.finding.warn{border-left-color:#D9A53B;background:linear-gradient(90deg,#FFFBF0,#fff 42%)}
.finding.bad{border-left-color:#D0607A;background:linear-gradient(90deg,#FFF6F8,#fff 42%)}
.finding.info{border-left-color:#4B8FD6;background:linear-gradient(90deg,#F5FAFF,#fff 42%)}
.finding-head{display:flex;align-items:center;gap:9px}
.level{padding:3px 8px;border-radius:999px;color:#50647B;background:#EEF3F8;font-size:10px;font-weight:800}
.finding.good .level{color:#167057;background:#E7F7F1}.finding.warn .level{color:#8A6519;background:#FFF4D9}
.finding.bad .level{color:#9B3750;background:#FDEBF0}.finding.info .level{color:#245B9B;background:#E6F1FF}
.finding p{margin:9px 0 0;color:#54697F;font-size:11px;line-height:1.7}
.action{display:flex;gap:9px;margin-top:11px;padding-top:10px;border-top:1px solid #EEF3F8}
.action b{flex:none;color:#2572C6;font-size:10px}.action span{color:#3C5470;font-size:11px}
.protocol{padding:24px 26px;border:1px solid var(--line);border-radius:18px;background:rgba(255,255,255,.9);box-shadow:0 18px 50px rgba(28,75,130,.07);margin-bottom:20px}
.protocol-head{display:flex;align-items:flex-start;justify-content:space-between;gap:20px;margin-bottom:16px}
.protocol-head h3{font-size:17px;margin-bottom:6px}
.pill{flex:none;padding:6px 11px;border-radius:999px;color:#315E91;background:#E7F1FD;font-size:10px;font-weight:700}
.split{display:flex;flex-wrap:wrap;gap:10px 22px;margin-bottom:15px;padding:10px 14px;border-radius:10px;background:#F2F7FD;color:#5D7189;font-size:10px}
.split span:first-child{font-weight:800;color:#245B9B}
.reference{display:grid;grid-template-columns:auto minmax(0,1fr);align-items:center;gap:18px;margin-bottom:15px;padding:13px 16px;border:1px solid var(--line);border-radius:12px;background:#F6F9FC}
.reference b{display:block;color:#153B68;font:700 21px/1 Georgia,serif}
.reference span,.reference small{display:block;color:var(--muted);font-size:10px}
.reference p{margin:0;color:#5A7089;font-size:11px}
table{width:100%;border-collapse:collapse}
th,td{padding:8px 9px;border-bottom:1px solid #EEF3F8;text-align:left;font-size:11px}
th{color:var(--muted);font-weight:400;font-size:10px}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.strong{font-weight:750;color:#153B68}
td.name{white-space:nowrap;color:#2D4A6B}
td.delta{white-space:nowrap;font-size:10px}
td.delta.better{color:#167057}td.delta.worse{color:#9B3750}td.delta.same{color:#7B8B9C}
td.graph,th.graph{width:20%}
tr.is-baseline td{background:#F7FBFF}
tr.group th{padding:14px 9px 6px;color:#1D4B80;font-size:11px;font-weight:800;border-bottom:1px solid #DBE7F3}
tr.group th .blurb{margin-left:9px;color:#8194A8;font-weight:400;font-size:10px}
.bar{height:6px;border-radius:9px;background:#E9EFF5;overflow:hidden}
.bar span{display:block;height:100%;border-radius:9px}
.foot{margin:10px 0 0;color:#7B8B9C;font-size:10px;line-height:1.7}
.method{margin:0;padding-left:20px;color:#40566E;font-size:11px;line-height:1.75}
.method li{margin-bottom:9px}.method b{color:#173B66}
footer{margin-top:32px;color:var(--muted);font-size:10px;line-height:1.9}
@media(max-width:900px){.cards{grid-template-columns:repeat(2,1fr)}td.graph,th.graph{display:none}.reference{grid-template-columns:1fr}}
"""


def render(view: dict) -> str:
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not view.get("available"):
        body = (
            f'<section class="protocol"><h3>{e(view.get("reason"))}</h3>'
            f'<p class="muted">{e(view.get("detail"))}</p>'
            f'<p class="foot mono">{e(view.get("command"))}</p></section>'
        )
    else:
        body = (
            findings_block(view)
            + "".join(protocol_block(protocol) for protocol in view["protocols"])
            + method_block(view)
        )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>行为图谱检索 · 实验看板</title>
<style>{STYLE}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>行为图谱检索 · 实验看板</h1>
    <p class="muted">图谱 {e(view.get('graph_cases') or '—')} 条案例 · 生成于 {e(generated)}</p>
    <p class="source mono">{e(view.get('path'))}</p>
  </header>
  {headline_block(view) if view.get('headline') else ''}
  {body}
  <footer>
    重新生成：<span class="mono">.venv/bin/python -m tools.experiments.bench_behavior_retrieval</span>
    &nbsp;→&nbsp;<span class="mono">.venv/bin/python -m tools.experiments.render_behavior_dashboard</span><br />
    同一个投影也渲染在 WebUI 的「实验看板」分区，两者结论一致。
  </footer>
</div>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    view = ExperimentConsoleQuery(args.report).behavior_retrieval()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(view), encoding="utf-8")
    print(f"wrote {args.output}")
    if not view.get("available"):
        print(f"  note: {view.get('reason')} — {view.get('detail')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
