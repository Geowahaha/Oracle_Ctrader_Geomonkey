from pathlib import Path


def test_fibo_mtf_shadow_scheduler_section_has_no_direct_shadow_live_promotion():
    text = Path("scheduler.py").read_text()
    start = text.index("    def _run_fibo_mtf_shadow_scan")
    end = text.index("    def _run_fibo_advance_scan", start)
    section = text[start:end]

    assert "_maybe_execute_ctrader_signal(signal" not in section
    assert "promoted_to_live" not in section
    assert "source=\"fibo_mtf_shadow\"" not in section
    assert "annotate_signal_with_fibo_mtf_plan" in section
    assert "_store_shadow_signal" in section
    assert "_maybe_execute_fibo_mtf_micro_live_probe" in section
    assert 'report["planner_errors"]' in section


def test_fibo_mtf_micro_live_adapter_strips_shadow_invariant_before_execution():
    text = Path("scheduler.py").read_text()
    start = text.index("    def _maybe_execute_fibo_mtf_micro_live_probe")
    end = text.index("    def _run_fibo_mtf_shadow_scan", start)
    section = text[start:end]

    assert "fibo_mtf_micro_live_adapter" in section
    assert 'live_raw["fibo_mtf_shadow"] = False' in section
    assert 'live_raw["fibo_mtf_live_enabled"] = True' in section
    assert "pattern = \"Fibo MTF Micro Live Probe\"" in section
    assert "_maybe_execute_ctrader_signal(live_signal" in section
