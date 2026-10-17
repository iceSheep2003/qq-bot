from pathlib import Path


CSS = Path("src/qqbot/interfaces/webui/static/app.css").read_text(encoding="utf-8")


def test_existing_workbench_surface_and_single_accent_are_preserved():
    assert ".sidebar" in CSS
    assert ".memory-workspace" in CSS
    assert "--accent:#1769e0" in CSS
    assert "--violet" in CSS  # compatibility token remains available to existing memory views


def test_operational_controls_have_tactile_and_keyboard_states():
    assert ".button:active" in CSS
    assert "scale(.98)" in CSS
    assert ":focus-visible" in CSS
    assert "touch-action:manipulation" in CSS


def test_data_surfaces_keep_dense_reading_order_without_layout_shift():
    assert ".data-panel" in CSS
    assert ".table-scroll" in CSS
    assert "position:sticky" in CSS
    assert "font-variant-numeric:tabular-nums" in CSS


def test_taste_layer_respects_reduced_motion_and_narrow_workbench():
    assert "prefers-reduced-motion:reduce" in CSS
    assert "@media(max-width:700px)" in CSS
    assert ".database-query textarea" in CSS
