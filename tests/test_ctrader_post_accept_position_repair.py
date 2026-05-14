import gc
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from analysis.signals import TradeSignal
from execution import ctrader_executor as ctrader_module


def _make_signal() -> TradeSignal:
    return TradeSignal(
        symbol="XAUUSD",
        direction="long",
        confidence=69.4,
        entry=4709.86,
        stop_loss=4706.52,
        take_profit_1=4712.87,
        take_profit_2=4713.87,
        take_profit_3=4715.20,
        risk_reward=1.2,
        timeframe="5m+1m",
        session="asian",
        trend="bullish",
        rsi=61.97,
        atr=6.0735,
        pattern="SCALP_FLOW_FORCE",
        reasons=["Force fallback mode: keep cadence 1 signal / 5m bar"],
        warnings=[],
        raw_scores={"signal_run_no": 38, "signal_run_id": "20260514051232-000038"},
        entry_type="limit",
    )


class CTraderPostAcceptPositionRepairTests(unittest.TestCase):
    def test_order_accepted_with_open_position_missing_sl_repairs_position_immediately(self):
        td = tempfile.mkdtemp()
        executor = None
        try:
            db_path = str(Path(td) / "ctrader_openapi.db")
            sig = _make_signal()
            with patch.object(ctrader_module.config, "CTRADER_ENABLED", True), \
                 patch.object(ctrader_module.config, "CTRADER_AUTOTRADE_ENABLED", True), \
                 patch.object(ctrader_module.config, "CTRADER_DRY_RUN", False), \
                 patch.object(ctrader_module.config, "CTRADER_DB_PATH", db_path), \
                 patch.object(ctrader_module.config, "XAU_GOVERNOR_V2_ENABLED", False), \
                 patch.object(ctrader_module.config, "CTRADER_ACCOUNT_ID", "46552794"), \
                 patch.object(ctrader_module.config, "CTRADER_ACCOUNT_LOGIN", "9900897"), \
                 patch.object(ctrader_module.config, "get_ctrader_allowed_sources", return_value={"scalp_xauusd"}), \
                 patch.object(ctrader_module.config, "get_ctrader_allowed_symbols", return_value={"XAUUSD"}), \
                 patch.object(ctrader_module.config, "get_ctrader_default_volume_symbol_overrides", return_value={}), \
                 patch.object(ctrader_module.CTraderExecutor, "sdk_available", new_callable=PropertyMock, return_value=True):
                executor = ctrader_module.CTraderExecutor()
                with patch.object(
                    executor,
                    "_run_worker",
                    return_value={
                        "ok": True,
                        "status": "accepted",
                        "message": "ctrader order_accepted",
                        "signal_symbol": "XAUUSD",
                        "broker_symbol": "XAUUSD",
                        "account_id": 46552794,
                        "order_id": 971000001,
                        "position_id": 620635949,
                        "volume": 1000.0,
                        "execution_meta": {
                            "execution_type": "ORDER_ACCEPTED",
                            "raw_execution": {
                                "position": {
                                    "positionId": "620635949",
                                    "price": 4701.43,
                                    "stopLoss": 0.0,
                                    "takeProfit": 4713.87,
                                }
                            },
                        },
                    },
                ), patch.object(
                    executor,
                    "amend_position_sltp",
                    return_value=ctrader_module.CTraderExecutionResult(
                        True,
                        "amended_position",
                        "ok",
                        signal_symbol="XAUUSD",
                        broker_symbol="XAUUSD",
                    ),
                ) as amend_mock, patch.object(
                    executor,
                    "_capture_after_execute",
                    return_value={"ok": False, "status": "capture_skipped"},
                ), patch.object(executor, "_reference_price", return_value=4701.43):
                    result = executor.execute_signal(sig, source="scalp_xauusd")

            self.assertTrue(result.ok)
            self.assertEqual(amend_mock.call_count, 1)
            amend_kwargs = amend_mock.call_args.kwargs
            self.assertEqual(int(amend_kwargs["position_id"]), 620635949)
            self.assertAlmostEqual(float(amend_kwargs["stop_loss"]), 4698.09, places=2)
            self.assertEqual(float(amend_kwargs["take_profit"]), 4713.87)
            repair = dict(result.execution_meta.get("protection_repair") or {})
            self.assertEqual(str(repair.get("action") or ""), "repair_position_protection")
            self.assertTrue(bool(repair.get("missing_stop_loss")))
        finally:
            executor = None
            gc.collect()
            shutil.rmtree(td, ignore_errors=True)

    def test_missing_sl_already_breached_closes_position_instead_of_invalid_amend(self):
        td = tempfile.mkdtemp()
        executor = None
        try:
            db_path = str(Path(td) / "ctrader_openapi.db")
            sig = _make_signal()
            with patch.object(ctrader_module.config, "CTRADER_ENABLED", True), \
                 patch.object(ctrader_module.config, "CTRADER_AUTOTRADE_ENABLED", True), \
                 patch.object(ctrader_module.config, "CTRADER_DRY_RUN", False), \
                 patch.object(ctrader_module.config, "CTRADER_DB_PATH", db_path), \
                 patch.object(ctrader_module.config, "XAU_GOVERNOR_V2_ENABLED", False), \
                 patch.object(ctrader_module.config, "CTRADER_ACCOUNT_ID", "46552794"), \
                 patch.object(ctrader_module.config, "CTRADER_ACCOUNT_LOGIN", "9900897"), \
                 patch.object(ctrader_module.config, "get_ctrader_allowed_sources", return_value={"scalp_xauusd"}), \
                 patch.object(ctrader_module.config, "get_ctrader_allowed_symbols", return_value={"XAUUSD"}), \
                 patch.object(ctrader_module.config, "get_ctrader_default_volume_symbol_overrides", return_value={}), \
                 patch.object(ctrader_module.CTraderExecutor, "sdk_available", new_callable=PropertyMock, return_value=True):
                executor = ctrader_module.CTraderExecutor()
                with patch.object(
                    executor,
                    "_run_worker",
                    return_value={
                        "ok": True,
                        "status": "accepted",
                        "message": "ctrader order_accepted",
                        "signal_symbol": "XAUUSD",
                        "broker_symbol": "XAUUSD",
                        "account_id": 46552794,
                        "order_id": 971000002,
                        "position_id": 620635950,
                        "volume": 1000.0,
                        "execution_meta": {
                            "execution_type": "ORDER_ACCEPTED",
                            "raw_execution": {
                                "position": {
                                    "positionId": "620635950",
                                    "price": 4701.43,
                                    "stopLoss": 0.0,
                                    "takeProfit": 4713.87,
                                }
                            },
                        },
                    },
                ), patch.object(executor, "_reference_price", return_value=4698.00), \
                     patch.object(executor, "amend_position_sltp") as amend_mock, \
                     patch.object(
                         executor,
                         "close_position",
                         return_value=ctrader_module.CTraderExecutionResult(True, "closed", "ok", signal_symbol="XAUUSD", broker_symbol="XAUUSD"),
                     ) as close_mock, \
                     patch.object(executor, "_capture_after_execute", return_value={"ok": False, "status": "capture_skipped"}):
                    result = executor.execute_signal(sig, source="scalp_xauusd")

            self.assertTrue(result.ok)
            self.assertEqual(amend_mock.call_count, 0)
            self.assertEqual(close_mock.call_count, 1)
            repair = dict(result.execution_meta.get("protection_repair") or {})
            self.assertEqual(str(repair.get("action") or ""), "emergency_close_missing_sl_breached_after_fill")
            self.assertEqual(int(repair.get("position_id") or 0), 620635950)
        finally:
            executor = None
            gc.collect()
            shutil.rmtree(td, ignore_errors=True)

    def test_missing_sl_repair_rejection_closes_position(self):
        td = tempfile.mkdtemp()
        executor = None
        try:
            db_path = str(Path(td) / "ctrader_openapi.db")
            sig = _make_signal()
            with patch.object(ctrader_module.config, "CTRADER_ENABLED", True), \
                 patch.object(ctrader_module.config, "CTRADER_AUTOTRADE_ENABLED", True), \
                 patch.object(ctrader_module.config, "CTRADER_DRY_RUN", False), \
                 patch.object(ctrader_module.config, "CTRADER_DB_PATH", db_path), \
                 patch.object(ctrader_module.config, "XAU_GOVERNOR_V2_ENABLED", False), \
                 patch.object(ctrader_module.config, "CTRADER_ACCOUNT_ID", "46552794"), \
                 patch.object(ctrader_module.config, "CTRADER_ACCOUNT_LOGIN", "9900897"), \
                 patch.object(ctrader_module.config, "get_ctrader_allowed_sources", return_value={"scalp_xauusd"}), \
                 patch.object(ctrader_module.config, "get_ctrader_allowed_symbols", return_value={"XAUUSD"}), \
                 patch.object(ctrader_module.config, "get_ctrader_default_volume_symbol_overrides", return_value={}), \
                 patch.object(ctrader_module.CTraderExecutor, "sdk_available", new_callable=PropertyMock, return_value=True):
                executor = ctrader_module.CTraderExecutor()
                with patch.object(
                    executor,
                    "_run_worker",
                    return_value={
                        "ok": True,
                        "status": "accepted",
                        "message": "ctrader order_accepted",
                        "signal_symbol": "XAUUSD",
                        "broker_symbol": "XAUUSD",
                        "account_id": 46552794,
                        "order_id": 971000003,
                        "position_id": 620635951,
                        "volume": 1000.0,
                        "execution_meta": {
                            "execution_type": "ORDER_ACCEPTED",
                            "raw_execution": {
                                "position": {
                                    "positionId": "620635951",
                                    "price": 4701.43,
                                    "stopLoss": 0.0,
                                    "takeProfit": 4713.87,
                                }
                            },
                        },
                    },
                ), patch.object(executor, "_reference_price", return_value=4701.20), \
                     patch.object(
                         executor,
                         "amend_position_sltp",
                         return_value=ctrader_module.CTraderExecutionResult(False, "amend_rejected", "invalid stop", signal_symbol="XAUUSD", broker_symbol="XAUUSD"),
                     ) as amend_mock, \
                     patch.object(
                         executor,
                         "close_position",
                         return_value=ctrader_module.CTraderExecutionResult(True, "closed", "ok", signal_symbol="XAUUSD", broker_symbol="XAUUSD"),
                     ) as close_mock, \
                     patch.object(executor, "_capture_after_execute", return_value={"ok": False, "status": "capture_skipped"}):
                    result = executor.execute_signal(sig, source="scalp_xauusd")

            self.assertTrue(result.ok)
            self.assertEqual(amend_mock.call_count, 1)
            self.assertEqual(close_mock.call_count, 1)
            repair = dict(result.execution_meta.get("protection_repair") or {})
            self.assertEqual(str(repair.get("action") or ""), "emergency_close_missing_sl_repair_failed")
            self.assertFalse(bool(repair.get("repair_ok")))
        finally:
            executor = None
            gc.collect()
            shutil.rmtree(td, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
