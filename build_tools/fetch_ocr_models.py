"""Fetch and verify the two pinned OCR models required for Windows packaging."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import urllib.request


ASSET_ROOT = Path(__file__).resolve().parents[1] / "build_assets" / "ocr"


def matches(path: Path, model: dict) -> bool:
    if not path.is_file() or path.stat().st_size != int(model["size"]):
        return False
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return digest.lower() == model["sha256"].lower()


def main() -> None:
    manifest = json.loads((ASSET_ROOT / "MODELS.json").read_text(encoding="utf-8"))
    target = ASSET_ROOT / "tessdata"
    target.mkdir(parents=True, exist_ok=True)
    for model in manifest["models"].values():
        filename = model["filename"]
        if filename != Path(filename).name:
            raise ValueError("The OCR manifest must contain simple filenames")
        destination = target / filename
        if matches(destination, model):
            print(f"Verified existing {filename}")
            continue
        with tempfile.NamedTemporaryFile(dir=target, suffix=".download", delete=False) as stream:
            temporary = Path(stream.name)
        try:
            with urllib.request.urlopen(model["url"], timeout=60) as response:
                with temporary.open("wb") as stream:
                    while chunk := response.read(1024 * 1024):
                        stream.write(chunk)
            if not matches(temporary, model):
                raise ValueError(f"Size or SHA-256 mismatch for {filename}")
            temporary.replace(destination)
            print(f"Downloaded and verified {filename}")
        finally:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
