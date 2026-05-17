from __future__ import annotations

import unittest

from config import Config
from execution.ctrader_executor import CTraderExecutor


class TradingCentralFamilyLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_lane_enabled = Config.DEXTER_TRADING_CENTRAL_FAMILY_LANE_ENABLED
        self._orig_family = Config.DEXTER_TRADING_CENTRAL_FAMILY_NAME
        self._orig_tokens = Config.DEXTER_TRADING_CENTRAL_SOURCE_TOKENS
        self._orig_active = Config.CTRADER_XAU_ACTIVE_FAMILIES

    def tearDown(self) -> None:
        Config.DEXTER_TRADING_CENTRAL_FAMILY_LANE_ENABLED = self._orig_lane_enabled
        Config.DEXTER_TRADING_CENTRAL_FAMILY_NAME = self._orig_family
        Config.DEXTER_TRADING_CENTRAL_SOURCE_TOKENS = self._orig_tokens
        Config.CTRADER_XAU_ACTIVE_FAMILIES = self._orig_active

    def test_ctrader_active_families_include_trading_central_when_enabled(self) -> None:
        Config.CTRADER_XAU_ACTIVE_FAMILIES = "xau_scalp_pullback_limit"
        Config.DEXTER_TRADING_CENTRAL_FAMILY_LANE_ENABLED = True
        Config.DEXTER_TRADING_CENTRAL_FAMILY_NAME = "xau_scalp_trading_central_intraday"

        families = Config.get_ctrader_xau_active_families()

        self.assertIn("xau_scalp_pullback_limit", families)
        self.assertIn("xau_scalp_trading_central_intraday", families)

    def test_source_family_maps_trading_central_token_when_enabled(self) -> None:
        Config.DEXTER_TRADING_CENTRAL_FAMILY_LANE_ENABLED = True
        Config.DEXTER_TRADING_CENTRAL_FAMILY_NAME = "xau_scalp_trading_central_intraday"
        Config.DEXTER_TRADING_CENTRAL_SOURCE_TOKENS = "trading_central,:tc:"

        family = CTraderExecutor._source_family("scalp_xauusd:tc:canary")
        self.assertEqual(family, "xau_scalp_trading_central_intraday")

    def test_source_family_ignores_trading_central_token_when_disabled(self) -> None:
        Config.DEXTER_TRADING_CENTRAL_FAMILY_LANE_ENABLED = False
        Config.DEXTER_TRADING_CENTRAL_SOURCE_TOKENS = "trading_central,:tc:"

        family = CTraderExecutor._source_family("scalp_xauusd:tc:canary")
        self.assertEqual(family, "xau_scalp_microtrend")


if __name__ == "__main__":
    unittest.main()
