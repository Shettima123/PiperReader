"""Install a verified source-built decoder only into an isolated build environment."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def validate_native_build(directory: Path) -> Path:
    directory = Path(directory)
    dll = directory / "libsndfile_x64.dll"
    manifest = read_json(directory / "build-manifest.json")
    actual = hashlib.sha256(dll.read_bytes()).hexdigest()
    if actual != manifest.get("dll_sha256"):
        raise ValueError("Native audio checksum does not match the build manifest")
    if (manifest.get("architecture") != "Windows x64"
            or manifest.get("library") != "libsndfile 1.2.2"):
        raise ValueError("Unexpected native audio build architecture or library")
    if read_json(directory / "sources.json") != read_json(ROOT / "build_tools/native_audio/sources.json"):
        raise ValueError("Native audio source recipe does not match this release")
    verification = read_json(directory / "verification.json")
    if verification.get("passed") is not True or verification.get("dll_sha256") != actual:
        raise ValueError("Native audio verification is absent, failed or belongs to another binary")
    return dll


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, nargs="?", default=ROOT / "build_assets/native_audio")
    args = parser.parse_args()
    dll = validate_native_build(args.directory)
    if sys.prefix == sys.base_prefix:
        raise RuntimeError("Use a dedicated virtual environment; global Python will not be modified")
    import _soundfile_data
    destination = Path(_soundfile_data.__file__).resolve().parent / dll.name
    if not destination.is_relative_to(Path(sys.prefix).resolve()):
        raise RuntimeError("The decoder destination must be inside the build virtual environment")
    shutil.copy2(dll, destination)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != hashlib.sha256(dll.read_bytes()).hexdigest():
        raise RuntimeError("Installed native audio checksum verification failed")
    print("Verified source-built native audio installed in the isolated build environment.")


if __name__ == "__main__":
    main()
