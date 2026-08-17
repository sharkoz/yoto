from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class YotoError(Exception):
    code: str
    message: str
    details: Any = None
    exit_code: int = 1

    def __str__(self) -> str:
        return self.message


def require(condition: bool, code: str, message: str, details: Any = None) -> None:
    if not condition:
        raise YotoError(code, message, details)
