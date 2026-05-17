import sqlite3
from pathlib import Path

from execution.xau_governor_v2 import GovernorConfig, XAUExposureGovernor, sl_distance_entropy


def _db(path: Path):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE ctrader_spot_ticks(id INTEGER PRIMARY KEY, symbol TEXT, bid REAL, ask REAL, event_utc TEXT)")
    con.execute("""CREATE TABLE ctrader_positions(
        position_id INTEGER, source TEXT, direction TEXT, volume REAL, entry_price REAL, stop_loss REAL, take_profit REAL,
        symbol TEXT, is_open INTEGER, first_seen_utc TEXT, last_seen_utc TEXT)
    """)
    con.execute("""CREATE TABLE ctrader_deals(
        deal_id INTEGER, position_id INTEGER, source TEXT, direction TEXT, symbol TEXT, pnl_usd REAL, execution_utc TEXT)
    """)
    con.execute("""CREATE TABLE xau_basket_snapshots(
        id INTEGER PRIMARY KEY, event_utc TEXT, combined_pnl REAL)
    """)
    con.execute("INSERT INTO ctrader_spot_ticks(symbol,bid,ask,event_utc) VALUES('XAUUSD',4680,4680.1,strftime('%Y-%m-%dT%H:%M:%SZ','now'))")
    return con


def test_sl_distance_entropy_detects_crowded_vs_distributed():
    assert sl_distance_entropy([10, 10, 10, 10, 10]) < 0.5
    assert sl_distance_entropy([5, 20, 50, 90, 130]) > 0.8


def test_governor_freezes_repeated_adverse_long_but_allows_short(tmp_path):
    db = tmp_path / "x.db"
    con = _db(db)
    for i in range(6):
        con.execute(
            "INSERT INTO ctrader_positions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (1000+i, 'fibo_xauusd', 'long', 100, 4690+i, 4685+i*0.01, 4800, 'XAUUSD', 1, '2026-05-06T16:00:00Z', '2026-05-06T16:10:00Z'),
        )
    con.execute("INSERT INTO xau_basket_snapshots(event_utc,combined_pnl) VALUES(datetime('now','-5 minutes'),-100)")
    con.execute("INSERT INTO xau_basket_snapshots(event_utc,combined_pnl) VALUES(datetime('now'),-160)")
    con.commit(); con.close()
    cfg = GovernorConfig(runtime_path=str(tmp_path/'state.json'), min_same_side=5, velocity_threshold_usd_per_min=-8, dryrun=False)
    gov = XAUExposureGovernor(db, cfg)
    v_long = gov.allow_entry(symbol='XAUUSD', direction='long', source='fibo_xauusd', confidence=80)
    assert not v_long.allowed
    assert any(g.startswith('G1_') for g in v_long.guards)
    v_short = gov.allow_entry(symbol='XAUUSD', direction='short', source='fibo_xauusd', confidence=80)
    assert v_short.allowed


def test_governor_uses_opening_direction_join_for_loss_streak_not_deal_direction(tmp_path):
    db = tmp_path / "x.db"
    con = _db(db)
    for i in range(2):
        con.execute("INSERT INTO ctrader_positions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (2000+i,'fibo_xauusd','long',100,4690,4680,4800,'XAUUSD',0,'2026-05-06T16:00:00Z','2026-05-06T16:10:00Z'))
        # deal.direction is short (closing side), but opening direction is long; governor must count long streak.
        con.execute("INSERT INTO ctrader_deals VALUES(?,?,?,?,?,?,strftime('%Y-%m-%dT%H:%M:%SZ','now'))", (3000+i,2000+i,'fibo_xauusd','short','XAUUSD',-5.0))
    con.commit(); con.close()
    cfg = GovernorConfig(runtime_path=str(tmp_path/'state.json'), loss_streak=2, dryrun=False)
    gov = XAUExposureGovernor(db, cfg)
    v_long = gov.allow_entry(symbol='XAUUSD', direction='long', source='fibo_xauusd')
    assert not v_long.allowed
    assert 'G4_lineage_cooldown' in v_long.guards
    v_short = gov.allow_entry(symbol='XAUUSD', direction='short', source='fibo_xauusd')
    assert v_short.allowed


def test_governor_shadow_dryrun_reports_guards_but_allows(tmp_path):
    db = tmp_path / "x.db"
    con = _db(db)
    for i in range(5):
        con.execute("INSERT INTO ctrader_positions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (4000+i,'fibo_xauusd','long',100,4690,4688,4800,'XAUUSD',1,'2026-05-06T16:00:00Z','2026-05-06T16:10:00Z'))
    con.execute("INSERT INTO xau_basket_snapshots(event_utc,combined_pnl) VALUES(datetime('now','-5 minutes'),0)")
    con.execute("INSERT INTO xau_basket_snapshots(event_utc,combined_pnl) VALUES(datetime('now'),-100)")
    con.commit(); con.close()
    cfg = GovernorConfig(runtime_path=str(tmp_path/'state.json'), mode='shadow', dryrun=True)
    gov = XAUExposureGovernor(db, cfg)
    v = gov.allow_entry(symbol='XAUUSD', direction='long', source='fibo_xauusd')
    assert v.allowed
    assert v.dryrun
    assert v.guards
    assert v.reason.startswith('dryrun:')


def test_g2_budget_freezes_when_same_side_loss_exceeds_budget_even_without_fast_velocity(tmp_path):
    db = tmp_path / "x.db"
    con = _db(db)
    for i in range(5):
        con.execute("INSERT INTO ctrader_positions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (5000+i,'fibo_xauusd','long',100,4700+i,4650,4800,'XAUUSD',1,'2026-05-06T16:00:00Z','2026-05-06T16:10:00Z'))
    con.execute("INSERT INTO xau_basket_snapshots(event_utc,combined_pnl) VALUES(strftime('%Y-%m-%dT%H:%M:%SZ','now','-5 minutes'),-50)")
    con.execute("INSERT INTO xau_basket_snapshots(event_utc,combined_pnl) VALUES(strftime('%Y-%m-%dT%H:%M:%SZ','now'),-51)")
    con.commit(); con.close()
    cfg = GovernorConfig(runtime_path=str(tmp_path/'state.json'), base_long_usd=10, dryrun=False)
    v = XAUExposureGovernor(db, cfg).allow_entry(symbol='XAUUSD', direction='long', source='fibo_xauusd')
    assert not v.allowed
    assert 'G2_exposure_budget_freeze' in v.guards


def test_stale_tick_freezes_new_entry_fail_safe(tmp_path):
    db = tmp_path / "x.db"
    con = _db(db)
    con.execute("UPDATE ctrader_spot_ticks SET event_utc=strftime('%Y-%m-%dT%H:%M:%SZ','now','-30 minutes')")
    con.commit(); con.close()
    cfg = GovernorConfig(runtime_path=str(tmp_path/'state.json'), stale_tick_max_age_sec=60, dryrun=False)
    v = XAUExposureGovernor(db, cfg).allow_entry(symbol='XAUUSD', direction='long', source='fibo_xauusd')
    assert not v.allowed
    assert 'stale_tick_entry_freeze' in v.guards
