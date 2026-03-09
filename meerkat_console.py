"""
meerkat_console.py -- Lightweight Meerkat Console reporting for Lambda functions.

Reports heartbeats, actions, and alerts to the Meerkat Console so the
Orchestrator can see what the X Posting Agent is doing. Uses only urllib
(no pip dependencies) to stay Lambda-compatible.

If MEERKAT_API_URL, MEERKAT_AGENT_ID, or MEERKAT_API_KEY are not set,
all functions silently return False. This means existing deployments
without Console env vars continue to work unchanged.

If the Meerkat API is unreachable, functions print a warning and return
False. They never raise exceptions or block the Lambda.
"""

import json
import os
import urllib.request
import urllib.error

# Read Console config from environment (optional, all three must be set)
_API_URL = os.environ.get("MEERKAT_API_URL", "").rstrip("/")
_AGENT_ID = os.environ.get("MEERKAT_AGENT_ID", "")
_API_KEY = os.environ.get("MEERKAT_API_KEY", "")

# Console reporting is enabled only when all three are configured
_ENABLED = bool(_API_URL and _AGENT_ID and _API_KEY)


def _post_event(event_payload):
    """Post an event to POST /v1/agents/{agentId}/events.

    Returns True on success, False on any failure.
    Never raises exceptions.
    """
    if not _ENABLED:
        return False

    url = f"{_API_URL}/v1/agents/{_AGENT_ID}/events"
    data = json.dumps(event_payload).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read().decode())
            return result.get("ok", False)
    except urllib.error.HTTPError as e:
        print(f"Meerkat Console: HTTP {e.code} posting event to {url}")
        return False
    except Exception as e:
        print(f"Meerkat Console: failed to post event: {e}")
        return False


def send_heartbeat(status="active", metrics=None):
    """Send a heartbeat so the Console knows this agent is alive."""
    return _post_event({
        "event_type": "heartbeat",
        "status": status,
        "metadata": metrics or {},
    })


def log_action(action, details=None, duration_ms=None, success=True):
    """Log a completed action (appears in Console chat)."""
    event = {
        "event_type": "action",
        "action": action,
        "details": details or {},
        "success": success,
    }
    if duration_ms is not None:
        event["duration_ms"] = duration_ms
    return _post_event(event)


def send_alert(severity, message, details=None):
    """Send an alert event (warning, error, critical)."""
    return _post_event({
        "event_type": "alert",
        "severity": severity,
        "message": message,
        "details": details or {},
    })
