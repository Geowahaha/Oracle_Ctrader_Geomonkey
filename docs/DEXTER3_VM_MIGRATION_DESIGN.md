# Dexter3 → Oracle VM: seamless 24/7 design (PC on/off irrelevant)

Owner goal (2026-07-10): the PC must not need to stay on. Compare transports,
pick, and design a seamless architecture.

## Transport comparison (evidence-based, this repo)

| | Local Desktop MCP (current) | cTrader OpenAPI (VM) | Remote MCP (workers) |
|---|---|---|---|
| Needs PC + desktop app on | ❌ YES — the whole problem | ✅ No — headless | ❌ YES (proxies back to a desktop MCP via tunnel — verified in `dexter-mcp/src/ctrader-proxy.ts`) |
| Battle-tested in this repo | Yes (dexter3, 5 days) | **Yes — main system trades on it 24/7 on this VM** (`dexter-monitor`, `ctrader-stream.service`, `ctrader-token-keepalive.timer`) | Read-only verified once |
| Failure modes seen | Session-table zombie (fixed 2026-07-09 but app still a SPOF), window off-screen, symbol reload gaps | OAuth token expiry (solved: keepalive timer), stream reconnect (solved: `execution/ctrader_stream.py`) | Worker session cache leak; extra hop; wrong account binding seen (46552794) |
| Latency to broker | ~60-200ms local | VM Singapore → broker: comparable | worst (2 hops) |
| Account | active-account risk (guard added) | token is ACCOUNT-SCOPED — pin ctidTraderAccountId=46670728 at connect ✅ stronger guarantee | bound to whatever the proxy holds |
| Verdict | dev/observation only | **WINNER — production home** | not a path |

## Target architecture

```
Oracle VM (24/7)
├── dexter-monitor.service        (main system — unchanged)
├── ctrader-stream.service        (OpenAPI data — unchanged)
├── ctrader-token-keepalive.timer (unchanged)
├── dexter3-fable.service   ← NEW: shadow_runner --live, transport=openapi
└── dexter3-grok.service    ← NEW: shadow_runner --live --grok, transport=openapi

Windows PC (optional, dev only)
└── local MCP lanes retired after cutover; cTrader Desktop = charts/manual only
```

Seamlessness = the lanes never live on the PC again. No dual-host handover
protocol, no split-brain risk: ONE home (VM), labels unchanged
(`dexter3:fable:m5h-v1`, `dexter3:grok-v1.0:scalper`), journal DB on VM.

## Phases

- **P1 (build, PC):** `dexter3/openapi_client.py` — adapter exposing the exact
  `Dexter3McpClient` surface, backed by the repo's proven OpenAPI primitives;
  `DEXTER3_TRANSPORT=local_mcp|openapi` factory in shadow_runner (default
  local_mcp = zero behavior change); field-shape parity is THE risk (openTime,
  netProfit, label, absolute SL/TP amend, volume units) — parity test compares
  live reads across both transports on the PC where both exist.
- **P2 (shadow, VM):** deploy branch; run both lanes with transport=openapi in
  DRY-RUN on VM; compare decisions/reads vs PC lanes for 1+ session.
- **P3 (cutover):** stop PC lanes while flat → start VM services live → PC off
  test: lanes keep trading. Rollback = reverse (services stop, PC launchers).
- **P4:** Telegram watcher moves to VM (systemd) so alerts survive PC off;
  Haiku watch brief updated to read via SSH or bridge.

## Risks / notes
- VM RAM 956MB, ~229MB free + 2GB swap: two loops ≈ 100-120MB — fits; watch OOM.
- OpenAPI symbol/volume conventions differ from local MCP (pipettes, cents on
  Remote-style APIs) — the adapter must normalize to the dict shapes dexter3
  already consumes; pin with golden-sample tests from REAL payload dumps
  (lesson: every MCP-field bug this week came from assumed field names).
- Account pinning: adapter refuses to start if token's ctidTraderAccountId ≠
  46670728 (demo 9922808) — stronger than the desktop guard.
