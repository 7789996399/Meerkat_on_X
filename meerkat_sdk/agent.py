import threading
import time
import platform
from typing import Optional, List, Dict

from .client import MeerkatClient
from .models import VerifyResult


class MeerkatAgent(MeerkatClient):
    """Full agent lifecycle client with heartbeats and Console integration.

    Extends MeerkatClient with agent registration, background heartbeats,
    delegation polling, and event logging for the Meerkat Console.
    """

    def __init__(
        self,
        api_key: str,
        agent_id: Optional[str] = None,
        name: Optional[str] = None,
        domain: str = "general",
        infrastructure: str = "unknown",
        base_url: str = "https://api.meerkatplatform.com",
        timeout: int = 30,
        heartbeat_interval: int = 60,
        auto_heartbeat: bool = True,
    ):
        super().__init__(
            api_key=api_key, base_url=base_url, timeout=timeout
        )
        self.agent_id = agent_id
        self.domain = domain
        self.heartbeat_interval = heartbeat_interval
        self._heartbeat_thread = None
        self._stop_heartbeat = threading.Event()
        self._last_delegation_ts = None

        # Register if no agent_id provided
        if not self.agent_id:
            if not name:
                raise ValueError("Either agent_id or name is required")
            reg = self._post("/v1/agents", {
                "name": name,
                "authorized_domains": [domain],
                "description": infrastructure,
            })
            self.agent_id = reg.get("agent_id") or reg.get("id")

        if auto_heartbeat:
            self._start_heartbeat()

    def verify(
        self,
        output: str,
        input: Optional[str] = None,
        context: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> VerifyResult:
        """Verify with the agent's default domain."""
        return super().verify(
            output=output,
            input=input,
            context=context,
            domain=self.domain,
            session_id=session_id,
        )

    # ── Console events ──────────────────────────────────────────

    def log_action(
        self,
        action: str,
        details: Optional[Dict] = None,
        duration_ms: Optional[int] = None,
        success: bool = True,
    ) -> bool:
        """Log an action event to the Console."""
        payload = {
            "event_type": "action",
            "action": action,
            "details": details or {},
            "success": success,
        }
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        return self._post_event(payload)

    def alert(
        self,
        message: str,
        severity: str = "info",
        details: Optional[Dict] = None,
    ) -> bool:
        """Send an alert to the Console."""
        return self._post_event({
            "event_type": "alert",
            "severity": severity,
            "message": message,
            "details": details or {},
        })

    def poll_delegations(self, limit: int = 10) -> List[Dict]:
        """Check for tasks delegated by the Console orchestrator."""
        params = {"eventType": "delegation", "limit": str(limit)}
        if self._last_delegation_ts:
            params["since"] = self._last_delegation_ts
        try:
            data = self._get(
                f"/v1/agents/{self.agent_id}/events", params=params
            )
            events = data.get("events", []) if isinstance(data, dict) else data
            if events:
                self._last_delegation_ts = events[0].get("timestamp")
            return events
        except Exception:
            return []

    # ── Heartbeat ───────────────────────────────────────────────

    def heartbeat(
        self,
        status: str = "active",
        metadata: Optional[Dict] = None,
    ) -> bool:
        """Send a single heartbeat event. Use this for batch jobs
        where auto_heartbeat=False."""
        payload = {
            "event_type": "heartbeat",
            "status": status,
            "metadata": metadata or {},
        }
        return self._post_event(payload)

    def _start_heartbeat(self):
        def _loop():
            while not self._stop_heartbeat.is_set():
                self._post_event({
                    "event_type": "heartbeat",
                    "status": "active",
                    "metadata": {"hostname": platform.node()},
                })
                self._stop_heartbeat.wait(self.heartbeat_interval)

        self._heartbeat_thread = threading.Thread(
            target=_loop, daemon=True, name="meerkat-heartbeat"
        )
        self._heartbeat_thread.start()

    def stop(self):
        """Stop the heartbeat thread."""
        self._stop_heartbeat.set()
        if self._heartbeat_thread:
            self._heartbeat_thread.join(timeout=5)

    def _post_event(self, payload: dict) -> bool:
        """Post an event to the agent's event log. Never raises."""
        if not self.agent_id:
            return False
        try:
            self._post(f"/v1/agents/{self.agent_id}/events", payload)
            return True
        except Exception:
            return False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.stop()
