"""The design layer is a rule set, not a mood.

Every check here corresponds to a failure that was visible in the rendered
console: surfaces boxed by borders and shadows, gradients standing in for
hierarchy, tiny uppercase labels above every heading, and a page that was one
card inside another card. Asserting the *last* declaration is deliberate — the
stylesheet is layered, so an earlier rule being correct proves nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path("src/qqbot/interfaces/webui/static")
CSS = (ROOT / "app.css").read_text(encoding="utf-8")
JS = (ROOT / "app.js").read_text(encoding="utf-8")
HTML = (ROOT / "index.html").read_text(encoding="utf-8")

MARKER = "Design layer: quiet panels, one accent, no decoration."
LAYER = CSS[CSS.index(MARKER):] if MARKER in CSS else ""


def last_declaration(prop: str, text: str = CSS) -> str:
    """The winning value for a custom property, i.e. the last one declared."""
    values = re.findall(rf"{re.escape(prop)}\s*:\s*([^;}}]+)", text)
    assert values, f"{prop} is never declared"
    return values[-1].strip()


def last_rule(selector: str, text: str = LAYER) -> str:
    """The winning declaration block for a selector, i.e. the last one."""
    rules = re.findall(rf"{re.escape(selector)}\{{([^}}]*)\}}", text)
    assert rules, f"no rule for {selector}"
    return rules[-1]


def test_the_design_layer_is_present_and_sits_last():
    assert LAYER, "the design layer block is missing from app.css"
    assert CSS.index(MARKER) > len(CSS) * 0.5


def test_one_ground_one_ink_one_accent():
    assert last_declaration("--ground").startswith("#")
    assert last_declaration("--ink").startswith("#")
    assert last_declaration("--accent").startswith("#")
    # The old theme's blue is still referenced by the token block it replaced,
    # but it must not be the value that wins.
    assert last_declaration("--accent") != "#1769e0"


def test_surfaces_are_flat_at_rest():
    assert last_declaration("--shadow") == "none"
    assert "box-shadow:none" in LAYER
    assert "backdrop-filter:none" in LAYER


def test_no_gradients_survive_in_the_winning_layer():
    declarations = re.findall(r"background(?:-image)?\s*:\s*([^;}]+)", LAYER)
    assert declarations, "the layer declares no backgrounds at all"
    assert not [item for item in declarations if "gradient" in item]


def test_the_page_is_a_ground_with_panels_not_a_card_in_a_card():
    body = last_rule(".memory-workspace")
    assert "background:none" in body
    assert "border:0" in body
    assert "box-shadow:none" in body
    assert "min-height:0" in body


def test_labels_are_not_tiny_uppercase_caps():
    assert re.search(r"\.eyebrow\{[^}]*text-transform:none", LAYER)
    assert re.search(r"\.eyebrow\{[^}]*letter-spacing:0", LAYER)
    # The decorative eyebrow is gone from the markup entirely: the heading below
    # it already says what the page is.
    assert 'class="eyebrow"' not in HTML
    assert 'class="eyebrow"' not in JS


def test_figures_are_aligned_and_set_in_tabular_numbers():
    assert "font-variant-numeric:tabular-nums" in LAYER
    assert re.search(r"td\.num,[^{]*\{[^}]*text-align:right", LAYER)


def test_motion_is_one_staggered_entrance_with_a_reduced_motion_fallback():
    assert "@keyframes rise" in LAYER
    assert "animation-delay:.06s" in LAYER
    assert "prefers-reduced-motion:reduce" in LAYER
    # Only transform and opacity animate.
    keyframes = re.search(r"@keyframes rise\{([^}]*)\}", LAYER).group(1)
    assert "transform" in keyframes and "opacity" in keyframes
    assert "height" not in keyframes and "width" not in keyframes


def test_navigation_folds_instead_of_vanishing_on_narrow_screens():
    compact = LAYER.replace(" ", "")
    assert ".nav-itemspan:last-child{display:inline" in compact
    assert ".sidebar{inset:auto000" in compact


def test_the_live_board_updates_in_place_instead_of_re_rendering():
    """Polling must not replace the DOM, or the board flashes every interval."""
    assert "function monitorSignature(" in JS
    assert "if (signature === monitorView.signature)" in JS
    assert "updateClocks(" in JS
    # The entrance animation is scoped to a mount-only class.
    assert ".entering>*{animation:rise" in CSS
    assert ".memory-workspace>*,.content>*{animation:rise" not in CSS
