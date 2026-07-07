#!/usr/bin/env python3
"""Restart cTrader desktop app on Windows when local MCP hangs (404)."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_FILE = ROOT / "data" / "runtime" / "ctrader_restart_state.json"
LOG_FILE = ROOT / "data" / "runtime" / "ctrader_restart.log"
MIN_RESTART_INTERVAL_SEC = 300
MCP_READY_WAIT_SEC = 55

# When this module is invoked from a windowless host (pythonw.exe via the
# scheduled watchdog), each console child (powershell/taskkill) would otherwise
# spawn its OWN visible console window — the cmd flashes the owner complained
# about. CREATE_NO_WINDOW suppresses that. No effect on non-Windows or on a
# GUI target's own window (cTrader still shows its normal UI).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def log_event(payload: dict) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now(timezone.utc).isoformat()} {json.dumps(payload, ensure_ascii=False)}\n"
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line)


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def find_ctrader_launcher() -> Path | None:
    """Prefer Start Menu shortcut — direct x64 exe often fails to bring MCP up."""
    shortcut = Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Raw Trading Ltd cTrader.lnk"
    if shortcut.exists():
        return shortcut
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        base = Path(local) / "Spotware" / "cTrader"
        direct = base / "cTrader.exe"
        if direct.exists():
            return direct
        if base.exists():
            candidates = sorted(base.rglob("cTrader.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
            if candidates:
                return candidates[0]
    return None


def list_ctrader_pids() -> list[int]:
    if sys.platform != "win32":
        return []
    ps = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -eq 'cTrader.exe' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW,
    )
    pids: list[int] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def stop_ctrader(timeout_sec: int = 20) -> list[int]:
    pids = list_ctrader_pids()
    if not pids:
        return []
    for pid in pids:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True, timeout=30, creationflags=_NO_WINDOW,
        )
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not list_ctrader_pids():
            return pids
        time.sleep(1)
    return pids


def start_ctrader(exe: Path) -> int | None:
    if exe.suffix.lower() == ".lnk":
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-Command", f'Start-Process -FilePath "{exe}"'],
            shell=False, creationflags=_NO_WINDOW,
        )
        return proc.pid
    proc = subprocess.Popen([str(exe)], cwd=str(exe.parent), shell=False, creationflags=_NO_WINDOW)
    return proc.pid


def wait_mcp_ready(wait_sec: int = MCP_READY_WAIT_SEC) -> dict:
    sys.path.insert(0, str(ROOT / "scripts"))
    from ctrader_mcp_client import CtraderMcpClient

    client = CtraderMcpClient(client_name="ctrader-restart-probe", client_version="1.0")
    deadline = time.time() + wait_sec
    last_err = "timeout"
    while time.time() < deadline:
        try:
            client.reset_session()
            health = client.ping_health()
            return {"ok": True, **health}
        except Exception as exc:
            last_err = str(exc)
            time.sleep(3)
    return {"ok": False, "error": last_err}


def restart_ctrader(*, force: bool = False, wait_sec: int = MCP_READY_WAIT_SEC) -> dict:
    now = time.time()
    state = load_state()
    last = float(state.get("last_restart_ts") or 0)
    if not force and last and (now - last) < MIN_RESTART_INTERVAL_SEC:
        return {
            "ok": False,
            "skipped": True,
            "reason": f"cooldown {int(MIN_RESTART_INTERVAL_SEC - (now - last))}s remaining",
            "last_restart": state.get("last_restart_utc"),
        }

    launcher = find_ctrader_launcher()
    if not launcher:
        return {"ok": False, "error": "cTrader launcher not found"}

    stopped = stop_ctrader()
    time.sleep(3)
    starter_pid = start_ctrader(launcher)
    health = wait_mcp_ready(wait_sec)

    result = {
        "ok": health.get("ok", False),
        "launcher": str(launcher),
        "stopped_pids": stopped,
        "starter_pid": starter_pid,
        "mcp": health,
        "restarted_at": datetime.now(timezone.utc).isoformat(),
    }
    state["last_restart_ts"] = now
    state["last_restart_utc"] = result["restarted_at"]
    state["last_result"] = result
    save_state(state)
    log_event(result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Ignore 5-minute restart cooldown")
    parser.add_argument("--wait", type=int, default=MCP_READY_WAIT_SEC, help="Seconds to wait for MCP")
    args = parser.parse_args()
    result = restart_ctrader(force=args.force, wait_sec=args.wait)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())