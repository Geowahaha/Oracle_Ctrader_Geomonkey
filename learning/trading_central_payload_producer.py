from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RAW_PATH = "data/runtime/trading_central_intraday_raw.json"
DEFAULT_TEXT_PATH = "data/runtime/trading_central_intraday_raw.txt"
DEFAULT_OUTPUT_PATH = "data/runtime/trading_central_intraday_signal.json"


class TradingCentralPayloadError(ValueError):
    pass


def _repo_path(path: str | Path) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return PROJECT_ROOT / p


def _first_present(payload: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in payload and payload.get(key) not in (None, ""):
            return payload.get(key)
    return None


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        val = float(value)
        return val if val > 0.0 else None
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    val = float(match.group(0))
    return val if val > 0.0 else None


def parse_direction(value: Any) -> str:
    token = str(value or "").strip().lower()
    if not token:
        return ""
    if token in {"long", "buy", "bull", "bullish", "up", "upside"}:
        return "long"
    if token in {"short", "sell", "bear", "bearish", "down", "downside"}:
        return "short"
    if re.search(r"\b(buy|bullish|upside|uptrend)\b", token):
        return "long"
    if re.search(r"\b(sell|bearish|downside|downtrend)\b", token):
        return "short"
    return ""


def _normalize_symbol(value: Any) -> str:
    token = str(value or "").strip().upper().replace("/", "")
    if token in {"XAU", "XAUUSD", "GOLD", "GOLDUSD"}:
        return "XAUUSD"
    if "GOLD" in token or "XAU" in token:
        return "XAUUSD"
    return token


def _parse_timestamp(value: Any, *, default_tz: str, now: datetime | None = None) -> str:
    if value in (None, ""):
        return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    text = str(value or "").strip()
    tz = ZoneInfo(default_tz or "UTC")
    iso_text = text.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        pass
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%y %H:%M", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.strptime(text, fmt).replace(tzinfo=tz)
            return dt.astimezone(timezone.utc).isoformat()
        except Exception:
            continue
    raise TradingCentralPayloadError(f"invalid_updated_at:{text}")


def _match_price(text: str, patterns: list[str]) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.MULTILINE)
        if match:
            return _parse_float(match.group(1))
    return None


def parse_panel_text(text: str) -> dict[str, Any]:
    body = str(text or "").strip()
    if not body:
        raise TradingCentralPayloadError("empty_panel_text")
    normalized = re.sub(r"[ \t]+", " ", body)
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    direction = ""
    for line in lines:
        if parse_direction(line):
            direction = parse_direction(line)
            break
    symbol = "XAUUSD" if re.search(r"\b(gold|xau(?:usd)?)\b", normalized, re.IGNORECASE) else ""
    updated_at = ""
    dt_match = re.search(r"\b(\d{1,2}/\d{1,2}/\d{2,4}\s+\d{1,2}:\d{2})\b", normalized)
    if dt_match:
        updated_at = dt_match.group(1)
    target = _match_price(
        normalized,
        [
            r"\btake profit(?:\s+at)?\s+([0-9][0-9,]*(?:\.\d+)?)",
            r"\btarget\b[^\d]{0,20}([0-9][0-9,]*(?:\.\d+)?)",
            r"\bgold\s*[-:]\s*([0-9][0-9,]*(?:\.\d+)?)",
        ],
    )
    stop_loss = _match_price(
        normalized,
        [
            r"\bstop loss(?:\s+at)?\s+([0-9][0-9,]*(?:\.\d+)?)",
            r"\bsl\b[^\d]{0,20}([0-9][0-9,]*(?:\.\d+)?)",
        ],
    )
    entry = _match_price(
        body,
        [
            r"(?im)^\s*(?:buy|sell)\s*$\s*^\s*([0-9][0-9,]*(?:\.\d+)?)\s*$",
            r"\b(?:entry|current price|price)\b[^\d]{0,20}([0-9][0-9,]*(?:\.\d+)?)",
        ],
    )
    return {
        "provider": "Trading Central",
        "analysis_type": "intraday" if re.search(r"\bintraday\b", normalized, re.IGNORECASE) else "",
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop_loss,
        "target": target,
        "updated_at": updated_at,
        "raw_text": body,
    }


def normalize_payload(
    raw: dict[str, Any],
    *,
    default_symbol: str = "XAUUSD",
    default_base_source: str = "scalp_xauusd",
    default_confidence: float | None = 74.0,
    default_tz: str = "Asia/Bangkok",
    now: datetime | None = None,
    source_path: str = "",
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TradingCentralPayloadError("payload_not_dict")
    text_payload = _first_present(raw, ["panel_text", "text", "raw_text"])
    source = dict(raw)
    if text_payload:
        parsed_text = parse_panel_text(str(text_payload))
        source = {**parsed_text, **{k: v for k, v in raw.items() if k not in {"panel_text", "text", "raw_text"}}}
    symbol = _normalize_symbol(_first_present(source, ["symbol", "instrument", "asset", "product"]) or default_symbol)
    if symbol != "XAUUSD":
        raise TradingCentralPayloadError(f"unsupported_symbol:{symbol or '-'}")
    direction = parse_direction(
        _first_present(source, ["direction", "side", "action", "bias", "recommendation", "signal"])
    )
    if direction not in {"long", "short"}:
        raise TradingCentralPayloadError("missing_direction")
    entry = _parse_float(_first_present(source, ["entry", "entry_price", "current_price", "price"]))
    stop_loss = _parse_float(_first_present(source, ["stop_loss", "sl", "stop"]))
    target = _parse_float(_first_present(source, ["target", "take_profit", "tp"]))
    pivot = _parse_float(_first_present(source, ["pivot", "invalidation", "key_level"]))
    if (entry is None) ^ (stop_loss is None):
        raise TradingCentralPayloadError("entry_stop_must_be_paired")
    if entry is not None and stop_loss is not None:
        if direction == "long" and stop_loss >= entry:
            raise TradingCentralPayloadError("invalid_long_stop")
        if direction == "short" and stop_loss <= entry:
            raise TradingCentralPayloadError("invalid_short_stop")
    if entry is not None and target is not None:
        if direction == "long" and target <= entry:
            raise TradingCentralPayloadError("invalid_long_target")
        if direction == "short" and target >= entry:
            raise TradingCentralPayloadError("invalid_short_target")
    conf_raw = _first_present(source, ["confidence", "score", "conviction"])
    confidence = _parse_float(conf_raw)
    if confidence is None and default_confidence is not None:
        confidence = float(default_confidence)
    updated_at = _parse_timestamp(
        _first_present(source, ["updated_at", "timestamp", "generated_at"]) or "",
        default_tz=default_tz,
        now=now,
    )
    updated_dt = datetime.fromisoformat(updated_at)
    signal_id = str(_first_present(source, ["signal_id", "id"]) or "").strip()
    if not signal_id:
        signal_id = f"tc-{symbol.lower()}-{updated_dt.strftime('%Y%m%dT%H%M%SZ')}-{direction}"
    out: dict[str, Any] = {
        "provider": str(_first_present(source, ["provider", "source_name", "publisher"]) or "Trading Central").strip(),
        "analysis_type": str(_first_present(source, ["analysis_type", "idea_type"]) or "intraday").strip().lower(),
        "timeframe": str(_first_present(source, ["timeframe", "horizon"]) or "intraday").strip().lower(),
        "symbol": symbol,
        "base_source": str(_first_present(source, ["base_source"]) or default_base_source).strip().lower(),
        "direction": direction,
        "entry_type": str(_first_present(source, ["entry_type", "order_type"]) or "limit").strip().lower(),
        "updated_at": updated_at,
        "signal_id": signal_id,
        "producer": "learning.trading_central_payload_producer",
    }
    for key, val in {
        "entry": entry,
        "stop_loss": stop_loss,
        "target": target,
        "pivot": pivot,
        "confidence": confidence,
    }.items():
        if val is not None:
            out[key] = round(float(val), 6)
    if source_path:
        out["producer_source_path"] = str(source_path)
    if text_payload:
        out["producer_input_type"] = "text"
    else:
        out["producer_input_type"] = "json"
    return out


def write_payload(payload: dict[str, Any], output_path: str | Path) -> Path:
    path = _repo_path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temp_path.replace(path)
    return path


def load_raw_payload(raw_file: str | Path, text_file: str | Path) -> tuple[dict[str, Any], str]:
    raw_path = _repo_path(raw_file)
    text_path = _repo_path(text_file)
    if raw_path.exists():
        try:
            payload = json.loads(raw_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise TradingCentralPayloadError(f"raw_json_invalid:{raw_path}:{exc}") from exc
        if not isinstance(payload, dict):
            raise TradingCentralPayloadError(f"raw_json_not_object:{raw_path}")
        return payload, str(raw_path)
    if text_path.exists():
        return {"panel_text": text_path.read_text(encoding="utf-8")}, str(text_path)
    raise TradingCentralPayloadError(f"input_missing:{raw_path}|{text_path}")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Normalize Trading Central intraday analysis into Dexter runtime payload.")
    parser.add_argument("--raw-file", default=os.getenv("TRADING_CENTRAL_PRODUCER_RAW_PATH", DEFAULT_RAW_PATH))
    parser.add_argument("--text-file", default=os.getenv("TRADING_CENTRAL_PRODUCER_TEXT_PATH", DEFAULT_TEXT_PATH))
    parser.add_argument("--output", default=os.getenv("TRADING_CENTRAL_PRODUCER_OUTPUT_PATH", DEFAULT_OUTPUT_PATH))
    parser.add_argument("--default-symbol", default=os.getenv("TRADING_CENTRAL_PRODUCER_DEFAULT_SYMBOL", "XAUUSD"))
    parser.add_argument("--default-base-source", default=os.getenv("TRADING_CENTRAL_PRODUCER_DEFAULT_BASE_SOURCE", "scalp_xauusd"))
    parser.add_argument("--default-tz", default=os.getenv("TRADING_CENTRAL_PRODUCER_DEFAULT_TZ", "Asia/Bangkok"))
    parser.add_argument(
        "--default-confidence",
        type=float,
        default=float(os.getenv("TRADING_CENTRAL_PRODUCER_DEFAULT_CONFIDENCE", "74.0")),
    )
    parser.add_argument("--print", action="store_true", dest="print_payload")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        raw, source_path = load_raw_payload(args.raw_file, args.text_file)
        payload = normalize_payload(
            raw,
            default_symbol=args.default_symbol,
            default_base_source=args.default_base_source,
            default_confidence=args.default_confidence,
            default_tz=args.default_tz,
            source_path=source_path,
        )
        output_path = write_payload(payload, args.output)
    except TradingCentralPayloadError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    if args.print_payload:
        print(json.dumps({"ok": True, "output_path": str(output_path), "payload": payload}, indent=2, sort_keys=True))
    else:
        print(json.dumps({"ok": True, "output_path": str(output_path), "signal_id": payload.get("signal_id")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
