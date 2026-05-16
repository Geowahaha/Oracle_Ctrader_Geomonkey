import json
import sqlite3
from pathlib import Path
from unittest.mock import patch, PropertyMock

from execution import ctrader_executor as ctrader_module


def test_exhausted_same_run_pair_demotes_to_probe_floor_not_block(tmp_path: Path):
    db_path = str(tmp_path / "ctrader_openapi.db")
    payload = {
        "symbol": "XAUUSD",
        "source": "scalp_xauusd:td:canary",
        "direction": "short",
        "entry": 5100.0,
        "stop_loss": 5095.0,
        "take_profit": 5103.0,
        "entry_type": "limit",
        "risk_usd": 2.5,
        "signal_run_id": "run-pair-probe",
        "raw_scores": {},
    }
    with patch.object(ctrader_module.config, "CTRADER_DB_PATH", db_path), \
         patch.object(ctrader_module.config, "CTRADER_XAU_PAIR_RISK_CAP_ENABLED", True), \
         patch.object(ctrader_module.config, "CTRADER_XAU_PAIR_RISK_MAX_USD", 3.0), \
         patch.object(ctrader_module.config, "CTRADER_XAU_PAIR_RISK_MIN_USD", 0.15), \
         patch.object(ctrader_module.config, "CTRADER_XAU_PAIR_RISK_CAP_FAMILIES", ""), \
         patch.object(ctrader_module.CTraderExecutor, "sdk_available", new_callable=PropertyMock, return_value=True):
        executor = ctrader_module.CTraderExecutor()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO execution_journal(created_ts,created_utc,source,status,symbol,signal_run_id,request_json)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    1.0,
                    "2026-03-19T13:53:24Z",
                    "scalp_xauusd:canary",
                    "accepted",
                    "XAUUSD",
                    "run-pair-probe",
                    json.dumps(
                        {
                            "symbol": "XAUUSD",
                            "source": "scalp_xauusd:canary",
                            "direction": "short",
                            "entry": 5100.0,
                            "stop_loss": 5095.0,
                            "take_profit": 5103.0,
                            "entry_type": "limit",
                            "risk_usd": 3.0,
                        }
                    ),
                ),
            )
            conn.commit()
        result = executor._apply_xau_same_run_pair_risk_cap(source="scalp_xauusd:td:canary", payload=payload)

    assert result["active"] is True
    assert result["blocked"] is False
    assert result["reason"] == "same_run_pair_probe_floor"
    assert payload["risk_usd"] == 0.15
    assert payload["raw_scores"]["xau_pair_risk_mode"] == "same_run_probe_floor"
    assert payload["raw_scores"]["xau_pair_risk_cap_applied"] is True
