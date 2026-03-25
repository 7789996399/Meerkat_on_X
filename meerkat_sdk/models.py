from dataclasses import dataclass, field
from typing import Optional, List, Dict


@dataclass
class ShieldResult:
    """Result from POST /v1/shield."""
    safe: bool = True
    threat_level: str = "NONE"
    threats: List[Dict] = field(default_factory=list)
    sanitized_input: Optional[str] = None
    audit_id: str = ""
    session_id: Optional[str] = None
    remediation: Optional[Dict] = None

    def __init__(self, **kwargs):
        defaults = {
            "safe": True, "threat_level": "NONE", "threats": [],
            "sanitized_input": None, "audit_id": "", "session_id": None,
            "remediation": None,
        }
        for k, v in defaults.items():
            setattr(self, k, kwargs.get(k, v))
        # Store any extra fields the API returns
        self._extra = {k: v for k, v in kwargs.items() if k not in defaults}


@dataclass
class VerifyResult:
    """Result from POST /v1/verify."""
    trust_score: int = 0
    status: str = "FLAG"
    severity: Optional[str] = None
    verification_mode: str = "heuristic"
    audit_id: str = ""
    session_id: Optional[str] = None
    attempt: int = 1
    remediation: Optional[Dict] = None
    checks: Dict = field(default_factory=dict)

    def __init__(self, **kwargs):
        defaults = {
            "trust_score": 0, "status": "FLAG", "severity": None,
            "verification_mode": "heuristic", "audit_id": "", "session_id": None,
            "attempt": 1, "remediation": None, "checks": {},
        }
        for k, v in defaults.items():
            setattr(self, k, kwargs.get(k, v))
        self._extra = {k: v for k, v in kwargs.items() if k not in defaults}

    @property
    def passed(self) -> bool:
        return self.status == "PASS"

    @property
    def flagged(self) -> bool:
        return self.status == "FLAG"

    @property
    def blocked(self) -> bool:
        return self.status == "BLOCK"


@dataclass
class AuditRecord:
    """Result from GET /v1/audit/:id."""
    audit_id: str = ""
    trust_score: Optional[int] = None
    status: Optional[str] = None
    session_id: Optional[str] = None
    session: Optional[Dict] = None

    def __init__(self, **kwargs):
        defaults = {
            "audit_id": "", "trust_score": None, "status": None,
            "session_id": None, "session": None,
        }
        for k, v in defaults.items():
            setattr(self, k, kwargs.get(k, v))
        self._extra = {k: v for k, v in kwargs.items() if k not in defaults}
