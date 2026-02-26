"""
api/bridge_server.py - Tiger Bridge API
HTTP + WebSocket server connecting Dexter Pro to the Web3 Dashboard.

Endpoints:
  GET  /api/signals/active   — Current active signals
  GET  /api/signals/history   — Past signals with outcomes
  GET  /api/performance       — Win rate, P&L, equity curve
  GET  /api/status            — System health
  WS   /ws/signals            — Real-time signal stream
"""
import asyncio
import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from aiohttp import web
except ImportError:
    web = None
    logger.warning("[BridgeAPI] aiohttp not installed - bridge API disabled")

try:
    from api.signal_store import signal_store
except Exception:
    signal_store = None

try:
    from execution.tiger_risk_governor import tiger_risk_governor
except Exception:
    tiger_risk_governor = None


class DexterBridgeServer:
    """Lightweight HTTP + WebSocket bridge server."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8787):
        self.host = host
        self.port = port
        self._ws_clients: list = []
        self._app: Optional[web.Application] = None
        self._runner: Optional[web.AppRunner] = None
        self._start_time = time.time()

    def _json_response(self, data: dict, status: int = 200) -> web.Response:
        return web.json_response(data, status=status, headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })

    # ─── HTTP Endpoints ───────────────────────────────────────────────

    async def handle_active_signals(self, request: web.Request) -> web.Response:
        """GET /api/signals/active"""
        if signal_store is None:
            return self._json_response({"error": "signal store unavailable"}, 503)
        signals = signal_store.get_active_signals(limit=20)
        return self._json_response({"signals": signals, "count": len(signals)})

    async def handle_signal_history(self, request: web.Request) -> web.Response:
        """GET /api/signals/history"""
        if signal_store is None:
            return self._json_response({"error": "signal store unavailable"}, 503)
        symbol = request.query.get("symbol")
        limit = min(int(request.query.get("limit", "50")), 200)
        signals = signal_store.get_signal_history(symbol=symbol, limit=limit)
        return self._json_response({"signals": signals, "count": len(signals)})

    async def handle_performance(self, request: web.Request) -> web.Response:
        """GET /api/performance"""
        if signal_store is None:
            return self._json_response({"error": "signal store unavailable"}, 503)
        stats = signal_store.get_performance_stats()
        equity_curve = signal_store.get_equity_curve(initial_equity=15.0)
        return self._json_response({
            "stats": stats,
            "equity_curve": equity_curve[-50:],  # last 50 points
        })

    async def handle_status(self, request: web.Request) -> web.Response:
        """GET /api/status"""
        uptime = time.time() - self._start_time
        status = {
            "status": "online",
            "uptime_seconds": round(uptime),
            "ws_clients": len(self._ws_clients),
            "signal_store": signal_store is not None,
            "tiger_governor": tiger_risk_governor is not None,
        }
        if tiger_risk_governor is not None:
            try:
                status["risk_phase"] = tiger_risk_governor.status(15.0)
            except Exception:
                pass
        return self._json_response(status)

    async def handle_risk_status(self, request: web.Request) -> web.Response:
        """GET /api/risk"""
        if tiger_risk_governor is None:
            return self._json_response({"error": "risk governor unavailable"}, 503)
        equity = float(request.query.get("equity", "15.0"))
        status = tiger_risk_governor.status(equity)
        return self._json_response(status)

    async def handle_cors_preflight(self, request: web.Request) -> web.Response:
        """OPTIONS handler for CORS preflight."""
        return web.Response(headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        })

    # ─── WebSocket ────────────────────────────────────────────────────

    async def handle_ws(self, request: web.Request) -> web.WebSocketResponse:
        """WS /ws/signals — Real-time signal stream."""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.append(ws)
        logger.info("[BridgeAPI] WebSocket client connected (%d total)", len(self._ws_clients))

        try:
            await ws.send_json({"type": "connected", "message": "Tiger Bridge active"})
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    # Echo back pings
                    if msg.data == "ping":
                        await ws.send_json({"type": "pong"})
                elif msg.type == web.WSMsgType.ERROR:
                    break
        finally:
            self._ws_clients.remove(ws)
            logger.info("[BridgeAPI] WebSocket client disconnected (%d remaining)", len(self._ws_clients))

        return ws

    async def broadcast_signal(self, signal_data: dict):
        """Broadcast a new signal to all connected WebSocket clients."""
        if not self._ws_clients:
            return
        msg = json.dumps({"type": "signal", "data": signal_data})
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_str(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.remove(ws)

    async def broadcast_performance(self):
        """Broadcast performance update to all connected clients."""
        if not self._ws_clients or signal_store is None:
            return
        stats = signal_store.get_performance_stats()
        msg = json.dumps({"type": "performance", "data": stats})
        dead = []
        for ws in self._ws_clients:
            try:
                await ws.send_str(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._ws_clients.remove(ws)

    # ─── Server Lifecycle ─────────────────────────────────────────────

    def create_app(self) -> "web.Application":
        """Create and configure the aiohttp application."""
        if web is None:
            raise ImportError("aiohttp is required for bridge API")

        app = web.Application()
        app.router.add_get("/api/signals/active", self.handle_active_signals)
        app.router.add_get("/api/signals/history", self.handle_signal_history)
        app.router.add_get("/api/performance", self.handle_performance)
        app.router.add_get("/api/status", self.handle_status)
        app.router.add_get("/api/risk", self.handle_risk_status)
        app.router.add_get("/ws/signals", self.handle_ws)

        # CORS preflight for all api routes
        app.router.add_route("OPTIONS", "/api/{tail:.*}", self.handle_cors_preflight)

        self._app = app
        return app

    async def start(self):
        """Start the bridge server."""
        app = self.create_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.host, self.port)
        await site.start()
        logger.info("[BridgeAPI] Tiger Bridge API running on http://%s:%d", self.host, self.port)

    async def stop(self):
        """Stop the bridge server."""
        if self._runner:
            await self._runner.cleanup()
        logger.info("[BridgeAPI] Tiger Bridge API stopped")


# Singleton (lazy start)
bridge_server = DexterBridgeServer()
