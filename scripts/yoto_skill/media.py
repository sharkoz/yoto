from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable

from mutagen.mp3 import MP3, HeaderNotFoundError

from .api import YotoAPI
from .errors import YotoError, require


@dataclass(frozen=True)
class AudioInfo:
    path: str
    filename: str
    sha256: str
    size: int
    duration: float
    title: str
    artist: str | None
    album: str | None

    def json(self) -> dict[str, Any]:
        return asdict(self)


def _first_tag(tags: Any, key: str) -> str | None:
    if not tags:
        return None
    value = tags.get(key)
    if not value:
        return None
    text = getattr(value, "text", None)
    if isinstance(text, list) and text:
        return str(text[0]).strip() or None
    return str(value).strip() or None


def inspect_mp3(path: str | Path) -> AudioInfo:
    source = Path(path).expanduser().resolve()
    require(source.is_file(), "audio_not_found", f"MP3 file does not exist: {source}")
    require(source.suffix.lower() == ".mp3", "unsupported_audio", "Only local MP3 files are supported")
    try:
        audio = MP3(source)
    except (HeaderNotFoundError, OSError) as exc:
        raise YotoError("invalid_mp3", f"Could not read MP3 metadata: {source}", str(exc)) from exc
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    title = _first_tag(audio.tags, "TIT2") or source.stem
    return AudioInfo(
        path=str(source),
        filename=source.name,
        sha256=digest.hexdigest(),
        size=source.stat().st_size,
        duration=round(float(audio.info.length), 3),
        title=title,
        artist=_first_tag(audio.tags, "TPE1"),
        album=_first_tag(audio.tags, "TALB"),
    )


def upload_audio(
    api: YotoAPI,
    info: AudioInfo,
    *,
    timeout: float = 300,
    poll_interval: float = 3,
    sleeper: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    request = api.audio_upload_url(info.sha256, info.filename)
    upload_id = request.get("uploadId") or request.get("upload_id")
    require(bool(upload_id), "upload_invalid_response", "Audio upload response has no upload ID", request)
    upload_url = request.get("uploadUrl") or request.get("upload_url")
    if upload_url:
        api.upload_signed(str(upload_url), Path(info.path).read_bytes(), "audio/mpeg")

    deadline = clock() + timeout
    latest: dict[str, Any] = {}
    while clock() < deadline:
        try:
            latest = api.transcoded_upload(str(upload_id))
        except YotoError as exc:
            status = exc.details.get("status") if isinstance(exc.details, dict) else None
            if status not in {404, 409, 425}:
                raise
            latest = {"status": "pending", "http_status": status}
        transcoded_hash = latest.get("transcodedSha256") or latest.get("transcoded_sha256")
        if transcoded_hash:
            return {
                "upload_id": str(upload_id),
                "transcoded_sha256": str(transcoded_hash),
                "track_url": f"yoto:#{transcoded_hash}",
                "deduplicated": not bool(upload_url),
                "transcoded_info": latest.get("transcodedInfo") or latest.get("transcoded_info") or {},
            }
        state = str(latest.get("status") or latest.get("state") or "").lower()
        if state in {"failed", "error", "rejected"}:
            raise YotoError("transcode_failed", "Yoto could not transcode the MP3", latest)
        sleeper(poll_interval)
    raise YotoError("transcode_timeout", "Timed out waiting for Yoto to transcode the MP3", latest)
