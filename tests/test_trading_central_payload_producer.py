from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from learning.trading_central_payload_producer import (
    TradingCentralPayloadError,
    normalize_payload,
    parse_panel_text,
    write_payload,
)


class TradingCentralPayloadProducerTests(unittest.TestCase):
    def test_parse_ctrader_panel_text(self) -> None:
        parsed = parse_panel_text(
            """
            Intraday
            Gold - 4830.00
            22/04/2026 16:11
            Buy
            4748.33
            Take profit at 4830 (8167.0 pips)
            Stop loss at 4715 (-3333.0 pips)
            Source: Trading Central
            """
        )

        self.assertEqual(parsed["symbol"], "XAUUSD")
        self.assertEqual(parsed["direction"], "long")
        self.assertAlmostEqual(float(parsed["entry"]), 4748.33, places=2)
        self.assertAlmostEqual(float(parsed["stop_loss"]), 4715.0, places=2)
        self.assertAlmostEqual(float(parsed["target"]), 4830.0, places=2)
        self.assertEqual(parsed["updated_at"], "22/04/2026 16:11")

    def test_normalize_json_payload(self) -> None:
        payload = normalize_payload(
            {
                "provider": "Trading Central",
                "symbol": "Gold",
                "recommendation": "Buy",
                "entry": "4748.33",
                "sl": "4715",
                "take_profit": "4830",
                "updated_at": "2026-04-22T16:11:00+07:00",
                "id": "tc-json-1",
            },
            now=datetime(2026, 4, 22, 9, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(payload["symbol"], "XAUUSD")
        self.assertEqual(payload["direction"], "long")
        self.assertEqual(payload["base_source"], "scalp_xauusd")
        self.assertEqual(payload["signal_id"], "tc-json-1")
        self.assertEqual(payload["updated_at"], "2026-04-22T09:11:00+00:00")
        self.assertAlmostEqual(float(payload["confidence"]), 74.0, places=2)

    def test_normalize_text_payload(self) -> None:
        payload = normalize_payload(
            {
                "panel_text": """
                Intraday
                Gold - 4830.00
                22/04/2026 16:11
                Buy
                4748.33
                Take profit at 4830
                Stop loss at 4715
                """
            },
            default_tz="Asia/Bangkok",
            now=datetime(2026, 4, 22, 9, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(payload["producer_input_type"], "text")
        self.assertEqual(payload["direction"], "long")
        self.assertEqual(payload["updated_at"], "2026-04-22T09:11:00+00:00")
        self.assertEqual(payload["signal_id"], "tc-xauusd-20260422T091100Z-long")

    def test_invalid_stop_is_rejected(self) -> None:
        with self.assertRaises(TradingCentralPayloadError):
            normalize_payload(
                {
                    "symbol": "XAUUSD",
                    "direction": "sell",
                    "entry": 4748.0,
                    "stop_loss": 4740.0,
                    "target": 4720.0,
                }
            )

    def test_write_payload_is_atomic_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "signal.json"
            written = write_payload({"signal_id": "tc-write-1", "direction": "long"}, out)

            self.assertEqual(written, out)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8"))["signal_id"], "tc-write-1")


if __name__ == "__main__":
    unittest.main()
