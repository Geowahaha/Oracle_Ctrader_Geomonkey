import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace


def _init_deals_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE ctrader_deals (
            deal_id INTEGER PRIMARY KEY,
            source TEXT DEFAULT '',
            symbol TEXT DEFAULT '',
            direction TEXT DEFAULT '',
            pnl_usd REAL DEFAULT 0,
            execution_utc TEXT DEFAULT ''
        )
        """
    )
    return conn


def _ts(minutes_ago):
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_side_throttle_is_directional_and_does_not_disable_family(tmp_path):
    from analysis.nonfibo_redesign import compute_side_throttle

    db = tmp_path / "ctrader_openapi.db"
    conn = _init_deals_db(db)
    rows = [
        (1, "xauusd_scheduled", "XAUUSD", "short", -3.0, _ts(1)),
        (2, "xauusd_scheduled", "XAUUSD", "short", -4.0, _ts(2)),
        (3, "xauusd_scheduled", "XAUUSD", "short", -5.0, _ts(3)),
        (4, "xauusd_scheduled", "XAUUSD", "long", 6.0, _ts(4)),
        (5, "xauusd_scheduled", "XAUUSD", "long", -1.0, _ts(5)),
    ]
    conn.executemany("INSERT INTO ctrader_deals VALUES (?,?,?,?,?,?)", rows)
    conn.commit(); conn.close()

    short = compute_side_throttle(db, source="xauusd_scheduled", direction="short", min_trades=3, loss_usd_trigger=8.0)
    long = compute_side_throttle(db, source="xauusd_scheduled", direction="long", min_trades=2, loss_usd_trigger=8.0)

    assert short.active is True
    assert short.size_mult == 0.30
    assert short.consecutive_losses == 3
    assert short.reason.startswith("rolling_side_bleed")
    assert long.active is False
    assert long.reason == "within_recent_side_tolerance"


def test_dynamic_confidence_floor_raises_for_weak_recent_window_and_lowers_for_strong(tmp_path):
    from analysis.nonfibo_redesign import compute_dynamic_confidence_floor

    db = tmp_path / "ctrader_openapi.db"
    conn = _init_deals_db(db)
    rows = []
    for i, pnl in enumerate([-3, -2, -1, -4, 5]):
        rows.append((i + 1, "scalp_xauusd:fss", "XAUUSD", "short", pnl, _ts(i + 1)))
    for i, pnl in enumerate([4, 3, 2, -1, 5], start=10):
        rows.append((i + 1, "scalp_xauusd:winner", "XAUUSD", "long", pnl, _ts(i + 1)))
    conn.executemany("INSERT INTO ctrader_deals VALUES (?,?,?,?,?,?)", rows)
    conn.commit(); conn.close()

    weak = compute_dynamic_confidence_floor(db, source="scalp_xauusd:fss", direction="short", base_floor=72.0, min_trades=5, window=5)
    strong = compute_dynamic_confidence_floor(db, source="scalp_xauusd:winner", direction="long", base_floor=72.0, min_trades=5, window=5)

    assert weak.active is True
    assert weak.floor == 77.0
    assert weak.reason.startswith("weak_recent_wr")
    assert strong.active is True
    assert strong.floor == 69.0
    assert strong.reason.startswith("strong_recent_wr")


def test_apply_size_multiplier_preserves_signal_and_records_telemetry():
    from analysis.nonfibo_redesign import apply_size_multiplier_to_signal

    signal = SimpleNamespace(raw_scores={"ctrader_risk_usd_override": 10.0})
    raw = apply_size_multiplier_to_signal(signal, multiplier=0.30, reason="rolling_side_bleed")

    assert raw["ctrader_risk_usd_override"] == 3.0
    assert raw["xau_rasg_risk_before_usd"] == 10.0
    assert raw["xau_rasg_risk_after_usd"] == 3.0
    assert raw["xau_rasg_reason"] == "rolling_side_bleed"
    assert signal.raw_scores is raw


def test_planned_rr_enforces_asymmetric_reward_check():
    from analysis.nonfibo_redesign import planned_rr

    assert planned_rr(3300.0, 3290.0, 3330.0, "long") == 3.0
    assert planned_rr(3300.0, 3310.0, 3270.0, "short") == 3.0
    assert planned_rr(3300.0, 3290.0, 3310.0, "long") == 1.0
    assert planned_rr(3300.0, 3300.0, 3330.0, "long") == 0.0
    assert planned_rr(3300.0, 3310.0, 3330.0, "long") == 0.0
    assert planned_rr(3300.0, 3290.0, 3270.0, "short") == 0.0
    assert planned_rr(3300.0, 3290.0, 0.0, "long") == 0.0


def test_source_mapping_scopes_to_nonfibo_xau_only():
    from analysis.nonfibo_redesign import is_nonfibo_xau_source

    assert is_nonfibo_xau_source("xauusd_scheduled") is True
    assert is_nonfibo_xau_source("scalp_xauusd:fss") is True
    assert is_nonfibo_xau_source("scalp_xauusd:winner") is True
    assert is_nonfibo_xau_source("fibo_xauusd") is False
    assert is_nonfibo_xau_source("scalp_btcusd:winner") is False
