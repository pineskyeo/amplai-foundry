"""Public, serializable failures. HOLD is not success and never coerced to False."""

from __future__ import annotations


class RuntimeFault(Exception):
    def __init__(
        self, code: str, message: str, *, outcome: str = "rejected", details: object = None
    ):
        super().__init__(message)
        self.code, self.message, self.outcome, self.details = code, message, outcome, details

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "outcome": self.outcome,
            "details": self.details,
        }


class Hold(RuntimeFault):
    def __init__(self, code: str, message: str, *, details: object = None):
        super().__init__(code, message, outcome="hold", details=details)


class Conflict(RuntimeFault):
    def __init__(self, code: str, message: str):
        super().__init__(code, message, outcome="rejected")
