from __future__ import annotations

import hashlib
import re
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError

from .api import YotoAPI
from .errors import YotoError, require
from .storage import app_data_dir


@dataclass(frozen=True)
class PreparedIcon:
    source: str
    icon: str
    preview: str
    width: int = 16
    height: int = 16
    mode: str = "RGBA"

    def json(self) -> dict[str, Any]:
        return asdict(self)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:40] or "song"


def prepare_icon(source: str | Path, title: str, root: Path | None = None) -> PreparedIcon:
    original = Path(source).expanduser().resolve()
    require(original.is_file(), "icon_not_found", f"Icon source does not exist: {original}")
    digest = hashlib.sha256(original.read_bytes()).hexdigest()[:12]
    destination = (root or app_data_dir() / "generated-icons") / f"{_slug(title)}-{digest}"
    destination.mkdir(parents=True, exist_ok=True)
    saved_source = destination / f"source{original.suffix.lower() or '.png'}"
    if original != saved_source:
        shutil.copy2(original, saved_source)
    icon_path = destination / "icon-16.png"
    preview_path = destination / "preview-256.png"
    try:
        with Image.open(original) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGBA")
            width, height = image.size
            side = min(width, height)
            left = (width - side) // 2
            top = (height - side) // 2
            image = image.crop((left, top, left + side, top + side))
            # Icons are displayed against black on Yoto Players. Composite first so
            # the upload is deterministic, opaque RGBA rather than palette PNG.
            background = Image.new("RGBA", image.size, (0, 0, 0, 255))
            background.alpha_composite(image)
            small = background.convert("RGB").resize((16, 16), Image.Resampling.LANCZOS)
            small = small.quantize(colors=32, method=Image.Quantize.MEDIANCUT).convert("RGBA")
            small.save(icon_path, format="PNG", optimize=True)
            small.resize((256, 256), Image.Resampling.NEAREST).save(preview_path, format="PNG")
    except (UnidentifiedImageError, OSError) as exc:
        raise YotoError("invalid_icon", f"Could not prepare icon image: {original}", str(exc)) from exc
    with Image.open(icon_path) as check:
        require(check.size == (16, 16) and check.mode == "RGBA", "invalid_icon_output", "Icon conversion failed")
    return PreparedIcon(str(saved_source), str(icon_path), str(preview_path))


def upload_icon(api: YotoAPI, prepared: PreparedIcon) -> dict[str, str]:
    path = Path(prepared.icon)
    response = api.upload_icon(path.name, path.read_bytes())
    icon = response.get("displayIcon", response)
    media_id = icon.get("mediaId") or icon.get("media_id")
    require(bool(media_id), "icon_upload_invalid_response", "Icon upload response has no media ID", response)
    return {"media_id": str(media_id), "icon_url": f"yoto:#{media_id}"}
