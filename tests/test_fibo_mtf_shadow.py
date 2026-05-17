from types import ModuleType, SimpleNamespace
import sys

sys.modules.setdefault("numpy", ModuleType("numpy"))
fake_pd = ModuleType("pandas")
fake_pd.DataFrame = object
sys.modules.setdefault("pandas", fake_pd)
fake_technical = ModuleType("analysis.technical")
fake_technical.TechnicalAnalysis = lambda: None
sys.modules.setdefault("analysis.technical", fake_technical)
fake_smc = ModuleType("analysis.smc")
fake_smc.SMCAnalyzer = lambda: None
fake_smc.SMCContext = object
fake_smc.LiquidityPool = object
sys.modules.setdefault("analysis.smc", fake_smc)

fake_fibonacci = ModuleType("analysis.fibonacci")
fake_fibonacci.FibonacciAnalyzer = lambda: None
sys.modules.setdefault("analysis.fibonacci", fake_fibonacci)
fake_market = ModuleType("market.data_fetcher")
fake_market.xauusd_provider = None
fake_market.session_manager = SimpleNamespace(get_session_info=lambda: {"active_sessions": []})
sys.modules.setdefault("market.data_fetcher", fake_market)

from scanners.fibo_mtf_shadow import (
    FiboMtfShadowScanner,
    FiboMtfSpec,
    _execution_anchor_payload,
    _series_values,
    apply_alignment_boosters,
    dedupe_by_parent_impulse,
    parent_chain_for_tf,
)
from analysis.signals import TradeSignal


class SimpleSeries:
    def __init__(self, values):
        self.values = list(values)
        self.iloc = self
    def __getitem__(self, idx):
        return self.values[idx]
    def tail(self, n):
        return SimpleSeries(self.values[-n:])
    def astype(self, _typ):
        return SimpleSeries([float(v) for v in self.values])
    def mean(self):
        return sum(self.values) / len(self.values) if self.values else 0.0
    def __sub__(self, other):
        return SimpleSeries([float(a) - float(b) for a, b in zip(self.values, other.values)])


class SimpleRow(dict):
    pass


class SimpleILoc:
    def __init__(self, df):
        self.df = df
    def __getitem__(self, idx):
        if isinstance(idx, slice):
            return SimpleDF(self.df.rows[idx])
        return SimpleRow(self.df.rows[idx])


class SimpleDF:
    def __init__(self, rows):
        self.rows = list(rows)
        self.empty = not self.rows
        self.iloc = SimpleILoc(self)
    def __len__(self):
        return len(self.rows)
    def __getitem__(self, key):
        return SimpleSeries([row[key] for row in self.rows])
    def tail(self, n):
        return SimpleDF(self.rows[-n:])
    def iterrows(self):
        for i, row in enumerate(self.rows):
            yield i, SimpleRow(row)


class FakeProvider:
    def __init__(self):
        self.calls = []

    def fetch(self, *, timeframe, bars):
        self.calls.append((timeframe, bars))
        rows = []
        price = 2300.0
        for i in range(90):
            open_p = price + i * 0.8
            close_p = open_p + 0.45
            rows.append({"open": open_p, "high": close_p + 0.3, "low": open_p - 0.3, "close": close_p, "volume": 100 + i})
        return SimpleDF(rows)


class FakeAnalyzer:
    def analyze(self, *, df_structure, df_entry, current_price, atr, smc_context=None):
        fib = SimpleNamespace(
            direction="bullish",
            swing_start=current_price - 12.0,
            swing_end=current_price + 8.0,
            impulse_strength=0.72,
            levels={0.382: current_price - 1.5, 0.5: current_price - 0.6, 0.618: current_price + 0.4},
            golden_pocket_low=current_price - 0.2,
            golden_pocket_high=current_price + 0.8,
        )
        return SimpleNamespace(
            fib_levels=fib,
            nearest_level_price=current_price - 2.0,
            nearest_level_ratio=0.618,
            retracement_depth=0.618,
            fibo_confluence_score=64.0,
            wave_phase="correction_end_resume",
            wave_confidence=0.71,
            correction_end_score=6.0,
            correction_end_confirmed=True,
        )


def test_mtf_shadow_scanner_emits_shadow_only_tf_payload():
    scanner = FiboMtfShadowScanner(
        provider=FakeProvider(),
        analyzer=FakeAnalyzer(),
        specs=[FiboMtfSpec("M1", "1m", "M1", "M5", 90)],
    )
    signals = scanner.scan()
    assert len(signals) == 1
    sig = signals[0]
    raw = sig.raw_scores
    assert sig.symbol == "XAUUSD"
    assert sig.direction == "long"
    assert sig.timeframe == "M1"
    assert raw["fibo_mtf_shadow"] is True
    assert raw["fibo_mtf_live_enabled"] is False
    assert raw["source"] == "fibo_xauusd"
    assert raw["display_source"] == "fibo_M1_xauusd"
    assert raw["tf_label"] == "M1"
    assert raw["ratio_zone"] == "near_0.618"
    assert raw["impulse_tf_stack"]["parent_tf"] == "M5"
    assert raw["parent_chain"] == ["M5", "M15", "M30"]
    assert raw["opportunity_first"] is True
    assert raw["tf_alignment_policy"] == "booster_not_gate"
    assert raw["execution_anchor_source"] == "recent_M1_structure"
    assert raw["execution_swing_low"] > 0
    assert raw["execution_swing_high"] > raw["execution_swing_low"]
    assert raw["execution_anchor_tf"] == "M1"
    assert raw["execution_anchor_is_live_plan"] is False
    assert raw["fibo_reclaim_is_live_plan"] is False
    assert raw["dema_14"] > 0
    assert raw["fibo_cluster_count"] >= 3
    assert isinstance(raw["fibo_reclaim_score"], float)


def _sig(pid, conf):
    return TradeSignal(
        symbol="XAUUSD", direction="long", confidence=conf, entry=2300, stop_loss=2290,
        take_profit_1=2310, take_profit_2=2320, take_profit_3=2330, risk_reward=3.0,
        timeframe="M1", session="", trend="", rsi=0, atr=1, pattern="x",
        raw_scores={"parent_impulse_id": pid, "impulse_state_confidence": conf / 100.0},
    )


class NumpyLikeValues:
    def __iter__(self):
        return iter(["1.25", "2.5"])
    def __bool__(self):
        raise ValueError("ambiguous truth value")


class PandasLikeSeries:
    values = NumpyLikeValues()


def test_series_values_handles_pandas_numpy_values_without_truthiness_check():
    assert _series_values(PandasLikeSeries()) == [1.25, 2.5]


def test_execution_anchor_payload_excludes_signal_bar_to_avoid_lookahead():
    rows = []
    for i in range(30):
        rows.append({"open": 2300 + i, "high": 2310 + i, "low": 2290 + i, "close": 2305 + i, "volume": 100})
    rows[-1]["high"] = 9999.0
    rows[-1]["low"] = 100.0

    payload = _execution_anchor_payload(FiboMtfSpec("M1", "1m", "M1", "M5", 90), SimpleDF(rows))

    assert payload["execution_anchor_source"] == "recent_M1_structure"
    assert payload["execution_swing_high"] < 9999.0
    assert payload["execution_swing_low"] > 100.0
    assert payload["execution_anchor_window"] == "pre_signal"


def test_parent_grouping_does_not_suppress_same_parent_impulse_opportunities():
    weak = _sig("H1:bull:100:200", 60)
    strong = _sig("H1:bull:100:200", 72)
    out = dedupe_by_parent_impulse([weak, strong])
    assert len(out) == 2
    assert not any(s.raw_scores.get("suppressed_duplicate") for s in out)
    assert {s.raw_scores.get("parent_impulse_group_size") for s in out} == {2}
    leaders = [s for s in out if s.raw_scores.get("parent_impulse_group_leader")]
    assert len(leaders) == 1
    assert leaders[0].confidence == 72


def test_alignment_booster_tags_agreeing_tfs_without_gating():
    m1 = _sig("p1", 60)
    m1.timeframe = "M1"
    m1.raw_scores["tf_label"] = "M1"
    m5 = _sig("p2", 63)
    m5.timeframe = "M5"
    m5.raw_scores["tf_label"] = "M5"
    h1 = _sig("p3", 70)
    h1.timeframe = "H1"
    h1.raw_scores["tf_label"] = "H1"
    out = apply_alignment_boosters([m1, m5, h1])
    for sig in out:
        raw = sig.raw_scores
        assert raw["alignment_booster"] is True
        assert raw["alignment_is_gate"] is False
        assert raw["aligned_tf_count"] == 3
        assert raw["aligned_tf_list"] == ["M1", "M5", "H1"]
        assert raw["opportunity_score"] >= sig.confidence


def test_parent_chain_is_multilevel_not_only_adjacent_tf():
    assert parent_chain_for_tf("M1") == ["M5", "M15", "M30"]
    assert parent_chain_for_tf("H1") == ["H4", "D1", "W1"]
    assert parent_chain_for_tf("W1") == []
