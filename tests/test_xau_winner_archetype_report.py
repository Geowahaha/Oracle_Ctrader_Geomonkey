import sqlite3
from pathlib import Path

from ops.xau_winner_archetype_report import (
    build_report,
    classify_archetype,
    classify_family,
    classify_risk_geometry,
    load_positions,
)


def _position(
    position_id=1,
    family="fibo_xauusd",
    direction="long",
    pnl_usd=10.0,
    entry=3300.0,
    stop=3290.0,
    tp=3330.0,
    first_seen="2026-05-12T08:00:00Z",
    close_utc="2026-05-12T09:30:00Z",
):
    return {
        "position_id": position_id,
        "symbol": "XAUUSD",
        "broker_symbol": "XAUUSD",
        "direction": direction,
        "entry_price": entry,
        "stop_loss": stop,
        "take_profit": tp,
        "volume": 1.0,
        "first_seen_utc": first_seen,
        "last_seen_utc": close_utc,
        "close_utc": close_utc,
        "label": f"dexter:XAUUSD:{family}:1",
        "comment": f"dexter|{family}|XAUUSD",
        "pnl_usd": pnl_usd,
        "deal_count": 2,
    }


def test_classify_family_prefers_label_and_comment_strategy_name():
    assert classify_family("dexter:XAUUSD:fibo_xauusd:37", "dexter|fibo_xauusd|XAUUSD") == "fibo_xauusd"
    assert classify_family("", "dexter|xauusd_scheduled:canary|XAUUSD") == "xauusd_scheduled"
    assert classify_family("manual", "") == "unknown"


def test_classify_archetype_separates_clean_fast_runner_and_ugly_winners():
    clean_fast = _position(pnl_usd=20, entry=3300, stop=3290, tp=3330)
    ugly = _position(pnl_usd=5, entry=3300, stop=3299.5, tp=3330)
    runner = _position(
        pnl_usd=40,
        entry=3300,
        stop=3290,
        tp=3360,
        first_seen="2026-05-12T01:00:00Z",
        close_utc="2026-05-12T08:00:00Z",
    )
    loser = _position(pnl_usd=-4)

    assert classify_archetype(clean_fast) == "clean_fast_winner"
    assert classify_archetype(ugly) == "false_good_or_ugly_winner"
    assert classify_archetype(runner) == "runner_winner"
    assert classify_archetype(loser) == "loser"


def test_classify_risk_geometry_flags_tiny_stop_and_rr_buckets():
    assert classify_risk_geometry(_position(entry=3300, stop=3299.5, tp=3330)) == "tiny_stop"
    assert classify_risk_geometry(_position(entry=3300, stop=3290, tp=3305)) == "sub_1r_target"
    assert classify_risk_geometry(_position(entry=3300, stop=3290, tp=3330)) == "healthy_2r_to_5r"
    assert classify_risk_geometry(_position(entry=3300, stop=3290, tp=3370)) == "wide_runner_target"


def test_build_report_groups_winners_by_family_session_archetype_and_risk_geometry():
    rows = [
        _position(position_id=1, family="fibo_xauusd", pnl_usd=20, first_seen="2026-05-12T08:00:00Z"),
        _position(position_id=2, family="fibo_xauusd", pnl_usd=-7, first_seen="2026-05-12T14:00:00Z"),
        _position(position_id=3, family="xauusd_scheduled", pnl_usd=12, first_seen="2026-05-12T02:00:00Z"),
    ]

    report = build_report(rows, top_n=2)

    assert report["summary"]["positions"] == 3
    assert report["summary"]["winners"] == 2
    assert report["summary"]["losers"] == 1
    assert report["by_family"]["fibo_xauusd"]["positions"] == 2
    assert report["by_family"]["fibo_xauusd"]["winners"] == 1
    assert report["by_session"]["london"]["positions"] == 1
    assert report["by_session"]["asia"]["positions"] == 1
    assert report["by_archetype"]["clean_fast_winner"]["winners"] == 2
    assert report["by_family_archetype"]["fibo_xauusd|clean_fast_winner"]["winners"] == 1
    assert report["by_family_archetype"]["fibo_xauusd|loser"]["losers"] == 1
    assert report["by_risk_geometry"]["healthy_2r_to_5r"]["positions"] == 3
    assert report["top_winners"][0]["pnl_usd"] == 20.0
    assert report["actionable_findings"][0]["kind"] in {"best_family", "worst_family", "best_session"}
    assert report["notes"][0].startswith("Read-only")


def test_load_positions_uses_positions_direction_and_sums_deals(tmp_path: Path):
    db = tmp_path / "trades.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE ctrader_positions (
            position_id INTEGER PRIMARY KEY,
            account_id INTEGER,
            source TEXT,
            lane TEXT,
            symbol TEXT,
            broker_symbol TEXT,
            direction TEXT,
            volume REAL,
            entry_price REAL,
            stop_loss REAL,
            take_profit REAL,
            label TEXT,
            comment TEXT,
            signal_run_id TEXT,
            signal_run_no INTEGER,
            journal_id INTEGER,
            is_open INTEGER,
            status TEXT,
            first_seen_utc TEXT,
            last_seen_utc TEXT,
            raw_json TEXT
        );
        CREATE TABLE ctrader_deals (
            deal_id INTEGER PRIMARY KEY,
            account_id INTEGER,
            position_id INTEGER,
            order_id INTEGER,
            source TEXT,
            lane TEXT,
            symbol TEXT,
            broker_symbol TEXT,
            direction TEXT,
            volume REAL,
            execution_price REAL,
            gross_profit_usd REAL,
            swap_usd REAL,
            commission_usd REAL,
            pnl_conversion_fee_usd REAL,
            pnl_usd REAL,
            outcome INTEGER,
            has_close_detail INTEGER,
            signal_run_id TEXT,
            signal_run_no INTEGER,
            journal_id INTEGER,
            execution_utc TEXT,
            raw_json TEXT
        );
        """
    )
    conn.execute(
        """
        INSERT INTO ctrader_positions
        VALUES (101, 1, '', '', 'XAUUSD', 'XAUUSD', 'long', 1, 3300, 3290, 3330,
                'dexter:XAUUSD:fibo_xauusd:1', 'dexter|fibo_xauusd|XAUUSD', '', 0,
                NULL, 0, 'closed', '2026-05-12T08:00:00Z', '2026-05-12T09:00:00Z', '{}')
        """
    )
    conn.execute(
        """
        INSERT INTO ctrader_deals
        VALUES (201, 1, 101, 1, '', '', 'XAUUSD', 'XAUUSD', 'short', 1, 3330,
                0, 0, 0, 0, 7.5, 0, 1, '', 0, NULL, '2026-05-12T09:00:00Z', '{}')
        """
    )
    conn.commit()
    conn.close()

    rows = load_positions(db, symbol="XAUUSD", days=30, limit=100)

    assert rows[0]["direction"] == "long"
    assert rows[0]["pnl_usd"] == 7.5
    assert rows[0]["family"] == "fibo_xauusd"
