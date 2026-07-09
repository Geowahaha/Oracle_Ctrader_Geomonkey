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

## P2 token/account investigation (2026-07-10 — Sonnet, read-only on VM)

**Q1 — the environment="live" contradiction, SOLVED, not a bug:**
`ops/ctrader_execute_once.py:390-392` hardcodes `mode == "accounts"` to
always use `EndPoints.PROTOBUF_LIVE_HOST` + report `environment="live"`,
**regardless of `CTRADER_USE_DEMO`** — this is deliberate and matches
cTrader's OpenAPI protocol requirement that `ProtoOAGetAccountListByAccessTokenReq`
(and app-auth) go over the Live host even for demo-account tokens; the
DEMO/LIVE branch in `_resolve_host()` (:161-174) and in
`execution/ctrader_stream.py:354-358` (the live stream service — confirmed
correctly branching on `CTRADER_USE_DEMO`) is unaffected. So `"environment":
"live"` in the earlier smoke failure said nothing about demo/live routing —
it is the *fixed* label for `mode=accounts` specifically. `sudo` was not the
cause either (config loads fine under sudo — confirmed via keepalive service
journal showing `[Config] Loaded: .env.local` every run).

**The REAL cause of "Invalid access token" — found via VM read-only checks:**
```
data/runtime/ctrader_token_state.json (redacted): last_refresh_utc=2026-07-01
06:01:51 UTC, refresh_count=3, consecutive_failures=1, saved_utc=2026-07-09
19:25:40 UTC (i.e. a refresh was ATTEMPTED and FAILED ~9 days after the last
SUCCESS — saved_utc moved, last_refresh_utc did not).
```
`sudo journalctl -u ctrader-token-keepalive.service` shows the SAME failure
every ~30 min since the timer last (re)started (`ctrader-token-keepalive.timer`
active since 2026-07-01 04:31:29 UTC, "1 week 1 day ago"):
`[TokenManager] refresh attempt 1-5/5: Access denied. Make sure the
credentials are valid.` — this is `Auth.refreshToken(refresh_token)` itself
being rejected by Spotware, i.e. **the persisted `refresh_token` is dead**
(revoked/rotated-away/expired), not a demo/live mismatch. App-level
credentials (client_id/secret) are fine — the earlier smoke test got past
`ProtoOAApplicationAuthReq` and failed specifically at
`ProtoOAGetAccountListByAccessTokenReq`, and the keepalive failure is at
`Auth.refreshToken`, both consistent with one root cause: this refresh_token
no longer works. This is chronic — the design doc's own note that this
"matches the `infra.auth_health` stale-token warnings on the board since
April" now has a concrete mechanism, not just a symptom.

**Token/account map (definitive parts; one part still unknown):**
- `api/ctrader_token_manager.py` is a **single process-wide singleton**,
  imported identically by `execution/ctrader_stream.py:37` (the live stream
  service) and `ops/ctrader_execute_once.py:27` (the worker dexter3's
  `openapi_client.py` shells out to) — there is only ONE token, not
  per-service tokens. `ctrader-stream.service` has been connected since
  `2026-07-01 05:18:47 UTC` (confirmed via `systemctl status`, "1 week 1 day
  ago") — i.e. it authenticated successfully using the token from just
  *before* it went stale, and has stayed up on that live TCP session ever
  since. It has NOT needed to re-authenticate. `dexter-monitor.service`
  restarted more recently (`2026-07-09 18:42:11 UTC`, ~50 min before this
  check) and is reported `active (running)` — but a restart does not prove
  it successfully re-authenticated a NEW cTrader OpenAPI session; it may
  simply not have needed one yet (MT5 path, or no cTrader order attempted
  since restart). **This is flagged as the single biggest risk below.**
- The static `Ctrader_accounts` / `CTRADER_ACCOUNTS_JSON` registry in
  `.env.local` (what `config.find_ctrader_account` searches) lists exactly
  4 accounts: `11955075`, `13079658`, `43880642` (all `live:true`, EUR/USD)
  and `46552794`/login `9900897` (`live:false`, the demo the earlier smoke
  test pinned against). **`46670728` (mission demo, login `9922808`) is NOT
  in this registry.** This is suggestive but NOT definitive proof the
  current OAuth token can't see it — this registry is a static cache (was
  populated at some past "accounts" call, unknown when) and the live,
  authoritative answer requires a fresh `ProtoOAGetAccountListByAccessTokenReq`
  call with a WORKING token, which we don't have right now (see above).
  **Cannot be determined until the refresh_token is replaced.**

**Q4 — account pin/selection code gap: DOES NOT EXIST, already shipped in P1.**
Re-verified every read/write method on `Dexter3OpenApiClient`
(`get_trendbars`, `get_spot_price`, `get_positions`, `get_balance`,
`get_pending_orders`, `get_deals`, `place_market_order`, `amend_position`,
`close_position`) routes through `self._payload()` (dexter3/openapi_client.py:395-399
pre-existing), which injects `account_id: self.account_id` into every
worker payload and calls `_ensure_account_pin()` first — none bypass it.
`self.account_id` already resolves from `DEXTER3_OPENAPI_ACCOUNT_ID` (env
override) or `DEFAULT_ACCOUNT_ID_PIN = 46670728` (:141-142, :257-261,
tests at `tests/test_dexter3_openapi_client.py:108-156`), and this is passed
via the worker's `--payload-file` JSON, which `ops/ctrader_execute_once.py::
_account_id_from_payload` reads as FIRST priority (:127-130) — ahead of
`config.CTRADER_ACCOUNT_LOGIN`/`CTRADER_ACCOUNT_ID`. **This means dexter3
can already select account 46670728 on a shared VM worker WITHOUT touching
the VM's global `CTRADER_ACCOUNT_ID`/`CTRADER_ACCOUNT_LOGIN` (which stays
pinned to 46552794 for the live main system).** No code change was needed
for the pin/selection mechanism itself.

**New code (additive, this session):** `Dexter3OpenApiClient.diagnose_account_pin()`
(dexter3/openapi_client.py, +76 lines) — a read-only, NEVER-raising preflight
that distinguishes the three failure shapes that were previously indistinguishable
without reading logs: `reason="pin_ok"`/`"pin_ok_cached"` (pin confirmed,
also caches like a real pin check), `reason="account_not_in_token_list"`
(broker reachable, token valid, but `account_id` isn't among the token's
accounts — a genuine mismatch), `reason="worker_call_failed"` with
`error_type` (`Dexter3OpenApiTransportError` vs plain `McpClientError` —
the "Invalid access token" case). Run it on the VM as a safe preflight
before starting any live/shadow loop:
```
sudo .venv/bin/python -c "from dexter3.openapi_client import Dexter3OpenApiClient as C; import json; print(json.dumps(C().diagnose_account_pin()))"
```
5 new tests (`tests/test_dexter3_openapi_client.py`, +77 lines): fresh
success, cached success skips the network round-trip, account-not-in-list,
tool-failure (`McpClientError`), transport-failure (`Dexter3OpenApiTransportError`).
**162/162 dexter3-related tests green** (`test_dexter3_openapi_client.py`,
`test_dexter3_wiring.py`, `test_dexter3_opening_manager.py`); full
`tests/test_dexter3_*.py` sweep: 878 passed, 1 pre-existing unrelated
failure (`test_dexter3_skipeval.py::test_fear_cost_summary_aggregates_evaluated_rows_only`
— date-hardcoded fixture now outside its 24h window as time has moved past
2026-07-05; reproduces identically on `git stash` with none of this
session's changes applied, so **not** a regression from this work).

**Q5 — P1 gaps #1 (symbol_details) / #2+#4 (deals label) phasing:**
- Gap #1 (`get_symbol_details` `NotImplementedError`) only blocks
  `place_market_order`/live entries (P3). **Does not block a decision-only
  P2 shadow run.**
- Gap #4 (`get_deals` label-blind) DOES corrupt P2's core purpose — a
  decision comparison — for any decision path that reads
  `dexter3.shadow_runner._lane_realized_today` (confirmed used at
  `shadow_runner.py:543-547` lane PnL, `:1360-1366` sizing multiplier,
  `:2171-2177` governor state): under `DEXTER3_TRANSPORT=openapi` this
  ALWAYS returns `realized=0` regardless of actual closed trades, so
  session-sizing/target-lock/ladder decisions WILL diverge from the
  `local_mcp` reference lane by design, not by bug. **Recommendation:**
  proceed with P2 shadow now, but treat any divergence traced to
  governor realized-PnL/sizing as EXPECTED and out-of-scope for the P2
  comparison (don't debug it as a P2 defect) until gap #4's
  `ProtoOAOrderListReq` label-join is built; entry-signal/technical
  decisions that don't touch the governor are valid to compare today.

**Biggest risk (this session's finding, not originally in scope but
surfaced by the read-only investigation):** the refresh_token used by
`api.ctrader_token_manager` (shared by the LIVE main system's
`ctrader-stream.service` and every `ops/ctrader_execute_once.py` worker
call, dexter3 included) has been rejected by Spotware every ~30 minutes for
at least 9 days (`Access denied. Make sure the credentials are valid.`).
The live stream is only alive because its TCP session pre-dates the
breakage and has never needed to re-authenticate. **If that session ever
drops (VM reboot, service restart, network blip) or if `dexter-monitor`
ever needs a fresh cTrader OpenAPI auth (e.g. to place a cTrader order),
reconnection will fail with the same "Access denied" the smoke test hit —
this threatens the LIVE main system, independent of dexter3.** Recommended
safest first step: PM/owner runs the manual re-authorization fallback in
`scripts/refresh_ctrader_token.py` (browser OAuth consent → paste code) to
mint a fresh access+refresh token pair, which `on_token_refreshed()`
persists to `data/runtime/ctrader_token_state.json` for every consumer to
pick up. This is a live-shared-credential change and is explicitly a
PM/owner action, not something this investigation executed.

## Risks / notes
- VM RAM 956MB, ~229MB free + 2GB swap: two loops ≈ 100-120MB — fits; watch OOM.
- OpenAPI symbol/volume conventions differ from local MCP (pipettes, cents on
  Remote-style APIs) — the adapter must normalize to the dict shapes dexter3
  already consumes; pin with golden-sample tests from REAL payload dumps
  (lesson: every MCP-field bug this week came from assumed field names).
- Account pinning: adapter refuses to start if token's ctidTraderAccountId ≠
  46670728 (demo 9922808) — stronger than the desktop guard.
