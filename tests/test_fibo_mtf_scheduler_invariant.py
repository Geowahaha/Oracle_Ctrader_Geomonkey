from pathlib import Path


def test_fibo_mtf_shadow_scheduler_section_has_no_direct_live_promotion():
    text = Path("scheduler.py").read_text()
    start = text.index("    def _run_fibo_mtf_shadow_scan")
    end = text.index("    def _run_fibo_advance_scan", start)
    section = text[start:end]

    assert "_maybe_execute_ctrader_signal" not in section
    assert "promoted_to_live" not in section
    assert "source=\"fibo_xauusd\"" not in section
    assert "annotate_signal_with_fibo_mtf_plan" in section
    assert "_store_shadow_signal" in section
    assert 'report["planner_errors"]' in section
