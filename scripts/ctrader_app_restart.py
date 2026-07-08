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


def _find_window_by_pid(target_pids: set[int]):
    """Enumerate top-level windows and return the first visible one owned by
    any of ``target_pids`` (ctypes only — no PowerShell/subprocess, works
    from a windowless pythonw host)."""
    if sys.platform != "win32" or not target_pids:
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in target_pids and user32.GetWindowTextLengthW(hwnd) > 0:
            found.append(hwnd)
            return False  # stop at first match
        return True

    user32.EnumWindows(_enum_proc, 0)
    return found[0] if found else None


def reposition_window_if_offscreen(process_name: str = "cTrader.exe", attempts: int = 12, delay_sec: float = 2.0) -> dict:
    """Permanent fix for the off-screen-window bug (2026-07-08): repeated
    watchdog ``taskkill /F`` cycles corrupt the OS-remembered window
    position, so the next launch can render the window thousands of pixels
    outside any monitor (observed: Top=-21333). This runs automatically
    after every restart — the owner should never need to fix it by hand
    again. Best-effort only: any failure here must never fail the restart
    (the trading loop's MCP connection does not depend on the window being
    visible), so every step is wrapped and swallowed.
    """
    if sys.platform != "win32":
        return {"ok": False, "reason": "not_windows"}
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        pids = set(list_ctrader_pids())
        if not pids:
            return {"ok": False, "reason": "no_process"}

        hwnd = None
        for _ in range(max(1, attempts)):
            hwnd = _find_window_by_pid(pids)
            if hwnd:
                break
            time.sleep(delay_sec)
        if not hwnd:
            return {"ok": False, "reason": "window_not_found_after_wait"}

        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        # SM_XVIRTUALSCREEN=76, SM_YVIRTUALSCREEN=77, SM_CXVIRTUALSCREEN=78,
        # SM_CYVIRTUALSCREEN=79 — the bounding box of ALL monitors combined,
        # so a legitimate multi-monitor window (which can have negative
        # coordinates for a monitor left/above the primary) is never
        # mistaken for the off-screen bug.
        vx = user32.GetSystemMetrics(76)
        vy = user32.GetSystemMetrics(77)
        vw = user32.GetSystemMetrics(78)
        vh = user32.GetSystemMetrics(79)

        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        on_screen = (
            rect.right > vx and rect.left < vx + vw
            and rect.bottom > vy and rect.top < vy + vh
        )
        was_iconic = bool(user32.IsIconic(hwnd))
        if on_screen and not was_iconic:
            return {"ok": True, "action": "already_on_screen", "rect": [rect.left, rect.top, rect.right, rect.bottom]}

        # SW_RESTORE = 9
        user32.ShowWindow(hwnd, 9)
        moved = bool(user32.MoveWindow(hwnd, 100, 100, 1400, 900, True))
        user32.SetForegroundWindow(hwnd)
        return {
            "ok": moved,
            "action": "repositioned",
            "was_off_screen": not on_screen,
            "was_minimized": was_iconic,
            "old_rect": [rect.left, rect.top, rect.right, rect.bottom],
        }
    except Exception as exc:  # noqa: BLE001 - never fail the restart over a window cosmetic
        return {"ok": False, "reason": f"exception:{exc}"}


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
    # Permanent fix (2026-07-08): always verify/repair the window position
    # after a restart, whether or not MCP came up healthy — a visible,
    # on-screen window is part of "restart succeeded" for the owner even if
    # this repo only strictly needs the MCP connection.
    window = reposition_window_if_offscreen()

    result = {
        "ok": health.get("ok", False),
        "launcher": str(launcher),
        "stopped_pids": stopped,
        "starter_pid": starter_pid,
        "mcp": health,
        "window": window,
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