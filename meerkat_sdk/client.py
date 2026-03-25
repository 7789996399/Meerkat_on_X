import time
import requests
from typing import Optional

from .models import ShieldResult, VerifyResult, AuditRecord
from .exceptions import MeerkatError, RateLimitError, AuthError


class MeerkatClient:
    """Thin client for the Meerkat verification API.

    Use this when you just need shield + verify + audit.
    For agent lifecycle (heartbeats, delegation, events), use MeerkatAgent instead.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.meerkatplatform.com",
        timeout: int = 30,
        max_retries: int = 3,
    ):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        })

    def shield(
        self,
        input: str,
        session_id: Optional[str] = None,
        sensitivity: Optional[str] = None,
    ) -> ShieldResult:
        """Scan content before your AI processes it.

        Args:
            input: Raw content to scan.
            session_id: Optional session ID to link with a verify call.
            sensitivity: "low", "medium", or "high".
        """
        payload = {"input": input}
        if session_id:
            payload["session_id"] = session_id
        if sensitivity:
            payload["sensitivity"] = sensitivity
        return ShieldResult(**self._post("/v1/shield", payload))

    def verify(
        self,
        output: str,
        input: Optional[str] = None,
        context: Optional[str] = None,
        domain: str = "general",
        session_id: Optional[str] = None,
    ) -> VerifyResult:
        """Verify AI-generated output against source context.

        Args:
            output: The AI-generated text to verify.
            input: The original user instruction or query.
            context: Source data to verify against (strongest mode).
            domain: One of: healthcare, legal, financial, pharma, general.
            session_id: Optional session ID for chaining with shield/retry.
        """
        payload = {"output": output, "domain": domain}
        if input:
            payload["input"] = input
        if context:
            payload["context"] = context
        if session_id:
            payload["session_id"] = session_id
        return VerifyResult(**self._post("/v1/verify", payload))

    def audit(
        self,
        audit_id: str,
        include_session: bool = False,
    ) -> AuditRecord:
        """Retrieve a verification audit record.

        Args:
            audit_id: The audit ID from a verify or shield response.
            include_session: If True, includes the full session correction chain.
        """
        params = {}
        if include_session:
            params["include"] = "session"
        return AuditRecord(**self._get(f"/v1/audit/{audit_id}", params=params))

    # ── HTTP internals ──────────────────────────────────────────

    def _post(self, path: str, payload: dict) -> dict:
        return self._request("POST", path, json=payload)

    def _get(self, path: str, params: dict = None) -> dict:
        return self._request("GET", path, params=params)

    def _request(self, method: str, path: str, **kwargs) -> dict:
        url = f"{self.base_url}{path}"
        last_error = None
        for attempt in range(self.max_retries):
            try:
                resp = self.session.request(
                    method, url, timeout=self.timeout, **kwargs
                )
                if resp.status_code == 429:
                    raise RateLimitError(
                        f"Rate limit exceeded: {resp.text}", resp
                    )
                if resp.status_code == 401:
                    raise AuthError("Invalid API key", resp)
                if resp.status_code >= 400:
                    raise MeerkatError(
                        f"API error {resp.status_code}: {resp.text}", resp
                    )
                return resp.json()
            except (requests.ConnectionError, requests.Timeout) as e:
                last_error = e
                if attempt < self.max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise MeerkatError(
                    f"Connection failed after {self.max_retries} retries"
                ) from e
        raise last_error  # pragma: no cover
