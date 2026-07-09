"""Dexter3 broker-transport factory (VM migration Phase 1).

See ``docs/DEXTER3_VM_MIGRATION_DESIGN.md``. ``make_client()`` is the ONLY
place dexter3 code should decide which broker client to construct — callers
must never construct ``Dexter3McpClient``/``Dexter3OpenApiClient`` directly
so a single env flip (``DEXTER3_TRANSPORT``) moves every lane at once.

``DEXTER3_TRANSPORT`` values:
    local_mcp (default) -> ``dexter3.mcp_client.Dexter3McpClient`` (unchanged
        behavior — this is byte-identical to every call site before this
        module existed: unset/empty/"local_mcp" all resolve here).
    openapi             -> ``dexter3.openapi_client.Dexter3OpenApiClient``
        (imported lazily so a Phase 1 desktop run with no cTrader OpenAPI
        credentials configured never even imports that module).

No arguments: both client classes have different constructor surfaces
(``Dexter3McpClient`` takes url/client_name/timeout knobs;
``Dexter3OpenApiClient`` takes worker_path/account_id knobs) and every
existing call site constructs with pure defaults today
(``Dexter3McpClient()``), so the factory intentionally does not accept
passthrough kwargs — add a dedicated env var on the target client instead if
a knob needs to be runtime-tunable.
"""
from __future__ import annotations

import os
from typing import Any

from dexter3.mcp_client import Dexter3McpClient

DEXTER3_TRANSPORT_ENV_VAR = "DEXTER3_TRANSPORT"
TRANSPORT_LOCAL_MCP = "local_mcp"
TRANSPORT_OPENAPI = "openapi"
VALID_TRANSPORTS = (TRANSPORT_LOCAL_MCP, TRANSPORT_OPENAPI)


def resolve_transport_name() -> str:
    """Return the normalized transport name from ``DEXTER3_TRANSPORT``.

    Unset or blank resolves to ``local_mcp`` (the pre-Phase-1 default).
    """
    raw = str(os.environ.get(DEXTER3_TRANSPORT_ENV_VAR, "") or "").strip().lower()
    return raw or TRANSPORT_LOCAL_MCP


def make_client() -> Any:
    """Construct the dexter3 broker client selected by ``DEXTER3_TRANSPORT``.

    Default (unset env, or explicit ``local_mcp``) returns a plain
    ``Dexter3McpClient()`` — identical to what every call site constructed
    before this factory existed. ``openapi`` returns a
    ``Dexter3OpenApiClient()`` (see ``dexter3/openapi_client.py`` for its
    documented gaps vs the local MCP surface). Any other value raises
    immediately (fail loud on a typo'd env var rather than silently falling
    back to a transport the operator didn't ask for).
    """
    transport = resolve_transport_name()
    if transport == TRANSPORT_LOCAL_MCP:
        return Dexter3McpClient()
    if transport == TRANSPORT_OPENAPI:
        from dexter3.openapi_client import Dexter3OpenApiClient

        return Dexter3OpenApiClient()
    raise ValueError(
        f"unknown {DEXTER3_TRANSPORT_ENV_VAR}={transport!r}; expected one of {VALID_TRANSPORTS}"
    )
