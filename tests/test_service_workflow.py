from __future__ import annotations

import copy

import pytest

from yoto_skill.errors import YotoError
from yoto_skill.icons import PreparedIcon
from yoto_skill.media import AudioInfo
from yoto_skill.service import YotoService


class MemoryAPI:
    def __init__(self, card) -> None:
        self.cards = {card["cardId"]: copy.deepcopy(card)}
        self.fail_save = False

    def list_cards(self):
        return [copy.deepcopy(card) for card in self.cards.values()]

    def get_card(self, card_id):
        return copy.deepcopy(self.cards[card_id])

    def save_card(self, card):
        if self.fail_save:
            raise YotoError("api_error", "mock update failed")
        saved = copy.deepcopy(card)
        card_id = saved.get("cardId") or "created-1"
        saved["cardId"] = card_id
        self.cards[card_id] = saved
        return copy.deepcopy(saved)

    def upload_cover(self, **_kwargs):
        return {"coverImage": {"mediaUrl": "https://yoto.example/cover.jpg"}}


def test_set_cover_preserves_existing_tracks(tmp_path, monkeypatch, simple_card) -> None:
    monkeypatch.setenv("YOTO_DATA_DIR", str(tmp_path / "data"))
    api = MemoryAPI(simple_card)
    before = copy.deepcopy(api.cards["card-1"]["content"])
    result = YotoService(api).set_cover("card-1", image_url="https://publisher.example/cover.jpg")
    assert result["cover_url"] == "https://yoto.example/cover.jpg"
    assert api.cards["card-1"]["content"] == before


@pytest.mark.parametrize("url", ["http://publisher.example/cover.jpg", "https://127.0.0.1/cover.jpg"])
def test_set_cover_rejects_nonpublic_url(tmp_path, monkeypatch, simple_card, url) -> None:
    monkeypatch.setenv("YOTO_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(YotoError) as error:
        YotoService(MemoryAPI(simple_card)).set_cover("card-1", image_url=url)
    assert error.value.code == "invalid_cover_url"


def test_mocked_complete_song_workflow(monkeypatch, tmp_path, simple_card) -> None:
    monkeypatch.setenv("YOTO_DATA_DIR", str(tmp_path / "data"))
    audio = AudioInfo("/tmp/song.mp3", "song.mp3", "hash", 1234, 9.5, "Metadata Title", "Artist", "Album")
    prepared = PreparedIcon("/tmp/source.png", "/tmp/icon.png", "/tmp/preview.png")
    monkeypatch.setattr("yoto_skill.service.inspect_mp3", lambda _: audio)
    monkeypatch.setattr("yoto_skill.service.prepare_icon", lambda *_: prepared)
    monkeypatch.setattr("yoto_skill.service.upload_icon", lambda *_: {"media_id": "icon-new", "icon_url": "yoto:#icon-new"})
    monkeypatch.setattr(
        "yoto_skill.service.upload_audio",
        lambda *_: {"upload_id": "up", "transcoded_sha256": "audio-new", "track_url": "yoto:#audio-new", "deduplicated": True, "transcoded_info": {}},
    )
    api = MemoryAPI(simple_card)
    service = YotoService(api)

    assert len(service.list_cards()) == 1
    added = service.add_song("card-1", "/tmp/song.mp3", "/tmp/source.png", "Third")
    key = added["song_key"]
    assert added["audio"]["deduplicated"] is True
    assert [song["title"] for song in added["card"]["songs"]] == ["First", "Second", "Third"]

    renamed = service.rename_song("card-1", key, "Third Renamed")
    assert renamed["songs"][-1]["title"] == "Third Renamed"
    moved = service.move_song("card-1", key, 1)
    assert moved["songs"][0]["song_key"] == key
    iconed = service.set_icon("card-1", key, "/tmp/new.png")
    assert iconed["card"]["songs"][0]["icon"] == "yoto:#icon-new"
    removed = service.remove_song("card-1", key, True)
    assert [song["song_key"] for song in removed["songs"]] == ["song-a", "song-b"]


def test_create_and_update_failure_do_not_corrupt_existing(monkeypatch, tmp_path, simple_card) -> None:
    monkeypatch.setenv("YOTO_DATA_DIR", str(tmp_path / "data"))
    api = MemoryAPI(simple_card)
    service = YotoService(api)
    created = service.create_card("Bedtime", "Quiet songs")
    assert created["title"] == "Bedtime"
    before = copy.deepcopy(api.cards["card-1"])
    api.fail_save = True
    with pytest.raises(YotoError, match="mock update failed"):
        service.rename_song("card-1", "song-a", "Unsaved")
    assert api.cards["card-1"] == before


def test_remove_requires_explicit_confirmation(monkeypatch, tmp_path, simple_card) -> None:
    monkeypatch.setenv("YOTO_DATA_DIR", str(tmp_path / "data"))
    with pytest.raises(YotoError) as error:
        YotoService(MemoryAPI(simple_card)).remove_song("card-1", "song-a", False)
    assert error.value.code == "confirmation_required"


def test_save_must_be_observable_on_refetch(monkeypatch, tmp_path, simple_card) -> None:
    monkeypatch.setenv("YOTO_DATA_DIR", str(tmp_path / "data"))

    class IgnoringAPI(MemoryAPI):
        def save_card(self, card):
            return copy.deepcopy(card)

    with pytest.raises(YotoError) as error:
        YotoService(IgnoringAPI(simple_card)).rename_song("card-1", "song-a", "Ignored")
    assert error.value.code == "update_not_verified"
