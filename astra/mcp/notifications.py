"""
Streaming context updates: relay daemon-side graph re-index events to a
connected MCP client as server-initiated notifications.

Uses the `astra/graph_updated` custom notification method (namespaced per
MCP convention for extension notifications, mirroring the "notifications/*"
pattern used by the spec's built-in notifications). The MCP Python SDK
(`mcp>=1.0.0`) supports server-initiated notifications via
`ServerSession.send_notification(...)`; we build a `mcp.types.Notification`
instance (rather than hand-rolling JSON-RPC framing) and hand it to that
API, which takes care of JSON-RPC envelope/transport details.

Status: the notification payload construction and the send call are fully
implemented and unit-testable in isolation (see tests/test_streaming.py).
End-to-end wiring — a background task in astra/mcp/server.py that connects
to the daemon's Unix socket (`~/.astra/daemon.sock`), issues `{"cmd":
"subscribe"}`, and forwards each broadcast `graph_delta` message through
this module to the live MCP ServerSession — is implemented as best-effort:
it depends on the daemon process being started separately (`astra daemon
start`) and on an MCP session already being live when a change lands. There
is no guaranteed delivery/replay if the notification is sent before a
client subscribes or during a connection gap; `graph_version` in tool
responses (astra/mcp/tools.py) is the reliable fallback for staleness
detection.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from mcp.types import Notification, NotificationParams

logger = logging.getLogger("astra.mcp.notifications")

GRAPH_UPDATED_METHOD = "astra/graph_updated"


class GraphUpdatedParams(NotificationParams):
    file: str
    symbols_changed: int
    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []
    graph_version: float = 0.0
    timestamp: float = 0.0


class GraphUpdatedNotification(Notification[GraphUpdatedParams, str]):
    method: str = GRAPH_UPDATED_METHOD
    params: GraphUpdatedParams


def build_graph_updated_notification(delta: dict) -> GraphUpdatedNotification:
    """
    Build an `astra/graph_updated` notification from a daemon delta dict
    (see astra.daemon.core.GraphDelta.to_dict / AstraDaemon._apply_delta).
    """
    added = delta.get("added", []) or []
    removed = delta.get("removed", []) or []
    changed = delta.get("changed", []) or []
    params = GraphUpdatedParams(
        file=delta.get("file", ""),
        symbols_changed=len(added) + len(removed) + len(changed),
        added=added,
        removed=removed,
        changed=changed,
        graph_version=delta.get("graph_version", 0.0),
        timestamp=delta.get("ts", time.time()),
    )
    return GraphUpdatedNotification(method=GRAPH_UPDATED_METHOD, params=params)


async def send_graph_updated_notification(session: Any, delta: dict) -> None:
    """
    Push an `astra/graph_updated` notification to a connected MCP client
    session. `session` is an `mcp.server.session.ServerSession` (or any
    object exposing an async `send_notification(notification)` method,
    which is how tests substitute a mock/fake session).
    """
    if session is None:
        logger.debug("No active MCP session; dropping graph_updated notification for %s",
                      delta.get("file"))
        return
    notification = build_graph_updated_notification(delta)
    try:
        await session.send_notification(notification)
    except Exception as e:
        logger.warning("Failed to send graph_updated notification: %s", e)
