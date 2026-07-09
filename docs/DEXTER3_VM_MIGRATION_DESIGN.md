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

## P1 status (2026-07-10 — built by Sonnet, PM-reviewed by Fable, 285 tests green)

Shipped: `dexter3/openapi_client.py` (770L), `dexter3/transport.py` factory
(`DEXTER3_TRANSPORT=local_mcp|openapi`, default local_mcp = byte-identical),
shadow_runner wired through factory (3-line diff), 41 new tests,
`scripts/dexter3_transport_parity.py` (read-only live diff, not yet run).

**Known gaps (P3 BLOCKERS until fixed):**
1. `get_deals` has NO label under OpenAPI (`ProtoOADeal` has no label field) →
   the Daily Mission Governor's per-label realized PnL is BLIND on this
   transport (target-lock/loss-stop/ladder dead). Fix: join via
   `ProtoOAOrderListReq` (orders carry the label). **Must fix before P3.**
2. `get_symbol_details` NotImplemented → `execute_entry` fail-closes (no
   entries possible) — safe for P2 shadow, blocker for P3.
3. `get_positions` lacks netProfit (no PnL field in proto) → basket manage
   degrades to hold (`unreliable=True`).
4. PM risk (Fable): adapter runs `ops/ctrader_execute_once.py` as a
   subprocess PER CALL = fresh OpenAPI TCP+OAuth connect per read. At 8s
   fast-tick this is connection churn against Spotware rate limits — the same
   churn antipattern as the MCP session zombie, one layer down. P2 must
   measure; the durable fix is a persistent OpenAPI daemon (extend
   ctrader-stream.service or a small local socket service) that the adapter
   calls instead of spawning workers.

## P2 smoke findings (2026-07-10 ~18:20Z — VM, real broker)

1. VM fast-forwarded a8498ab→c64c964 (65 commits; verified ZERO main-system
   files in the diff before pulling; dexter-monitor untouched/not restarted).
2. Adapter smoke on VM FAILED at the account pin — **by design** (fail-closed
   worked): `ops/ctrader_execute_once.py --mode accounts` returns
   `{"ok":false,"message":"Invalid access token","environment":"live",
   token_refresh: refresh_failed}`.
3. **Root discovery: the worker resolves environment from
   `config.CTRADER_USE_DEMO` (ops/ctrader_execute_once.py:168-174) — on the
   VM this resolves to LIVE, and the LIVE token is invalid/stale** (matches
   the `infra.auth_health` stale-token warnings on the board since April).
   The running `ctrader-stream.service` + token-keepalive presumably serve a
   different token path — must be mapped before P2 continues.
4. PC has no OpenAPI token at all ("No access token available") — parity
   script can only pass after a PC demo token exists OR parity moves to
   golden-sample mode.

**P2 queue (next Sonnet task, fresh session):**
- Map the VM token inventory: which token does ctrader-stream.service use,
  which does the worker use, where does scripts/refresh_ctrader_token.py +
  api/ctrader_token_manager.py read/write state, and what does
  ctrader-token-keepalive actually refresh.
- Wire the DEMO path: CTRADER_USE_DEMO=true for the dexter3 worker context +
  a valid demo-scoped token (account 46670728) with keepalive.
- Re-run the VM smoke: pin must PASS (traderId/account 46670728), then
  balance/positions/trendbars shapes vs golden samples.
- Only then: gaps #1/#2 (symbol_details worker mode, deals-label join) and
  the persistent-connection daemon (risk #4).

## Risks / notes
- VM RAM 956MB, ~229MB free + 2GB swap: two loops ≈ 100-120MB — fits; watch OOM.
- OpenAPI symbol/volume conventions differ from local MCP (pipettes, cents on
  Remote-style APIs) — the adapter must normalize to the dict shapes dexter3
  already consumes; pin with golden-sample tests from REAL payload dumps
  (lesson: every MCP-field bug this week came from assumed field names).
- Account pinning: adapter refuses to start if token's ctidTraderAccountId ≠
  46670728 (demo 9922808) — stronger than the desktop guard.
