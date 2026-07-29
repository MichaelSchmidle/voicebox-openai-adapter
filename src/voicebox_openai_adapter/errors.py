from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class AdapterError(Exception):
    status_code: int
    message: str
    error_type: str
    code: str
    headers: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def body(self) -> dict[str, dict[str, str]]:
        return {
            "error": {
                "message": self.message,
                "type": self.error_type,
                "code": self.code,
            }
        }
