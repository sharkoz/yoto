from __future__ import annotations

import copy
from typing import Any

import pytest


@pytest.fixture
def simple_card() -> dict[str, Any]:
    return {
        "cardId": "card-1",
        "title": "Road Trip",
        "unknownServerField": {"keep": True},
        "content": {
            "playbackType": "linear",
            "chapters": [
                {
                    "key": "song-a",
                    "title": "First",
                    "tracks": [
                        {
                            "key": "01",
                            "title": "First",
                            "trackUrl": "yoto:#audio-a",
                            "duration": 10.25,
                            "fileSize": 1000,
                            "display": {"icon16x16": "yoto:#icon-a"},
                        }
                    ],
                    "display": {"icon16x16": "yoto:#icon-a"},
                },
                {
                    "key": "song-b",
                    "title": "Second",
                    "tracks": [
                        {
                            "key": "01",
                            "title": "Second",
                            "trackUrl": "yoto:#audio-b",
                            "duration": 20,
                            "fileSize": 2000,
                        }
                    ],
                },
            ],
        },
        "metadata": {"custom": "preserved", "media": {"serverHint": 7}},
    }


class MemoryTokenStore:
    def __init__(self, value: dict[str, Any] | None = None) -> None:
        self.value = copy.deepcopy(value)
        self.saved: list[dict[str, Any]] = []

    def load(self, _account: str) -> dict[str, Any] | None:
        return copy.deepcopy(self.value)

    def save(self, _account: str, value: dict[str, Any]) -> None:
        self.value = copy.deepcopy(value)
        self.saved.append(copy.deepcopy(value))

    def delete(self, _account: str) -> None:
        self.value = None
