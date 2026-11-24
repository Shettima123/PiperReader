"""Collect pinned upstream sources and create a verified dependency archive.

This tool does not infer source provenance from an installed version number.
Every source URL, revision, byte count, and digest is reviewed in the manifest.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
from importlib.metadata import distribution
import json
from pathlib import Path
import re
import tempfile
import urllib.parse
import urllib.request
import zipfile


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "build_tools" / "third_party_sources.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "build_assets" / "sources"
COPYLEFT_PACKAGES = {
    "piper-tts", "phonemizer-fork", "pymupdf", "pystray", "edge-tts", "certifi",
}


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_manifest(data: dict) -> dict:
    if data.get("schema_version") != 1 or not isinstance(data.get("artifacts"), list):
        raise ValueError("Unsupported or missing source manifest schema")
    names = set()
    identifiers = set()
    for item in data["artifacts"]:
        name = item.get("filename", "")
        if not name or name in (".", "..") or re.search(r"[/\\:]", name):
            raise ValueError("Source manifest filenames must be simple relative names")
        if name.casefold() in names or item.get("id") in identifiers or not item.get("id"):
            raise ValueError("Duplicate or missing source artifact identifier/filename")
        names.add(name.casefold())
        identifiers.add(item["id"])
        if not re.fullmatch(r"[0-9a-fA-F]{64}", item.get("sha256", "")):
            raise ValueError(f"Missing or invalid SHA-256 for {name}")
        if not isinstance(item.get("size"), int) or item["size"] <= 0:
            raise ValueError(f"Missing or invalid byte count for {name}")
        url = urllib.parse.urlsplit(item.get("url", ""))
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError(f"Source URL must be credential-free HTTPS: {name}")
        if not isinstance(item.get("packages", {}), dict):
            raise ValueError(f"Invalid package mapping for {name}")
    for item in data["artifacts"]:
        for required in item.get("requires", []):
            if required not in identifiers:
                raise ValueError(f"Required native/build source {required} is missing for {item['id']}")
    return data


def load_manifest(path: Path) -> dict:
    return validate_manifest(json.loads(Path(path).read_text(encoding="utf-8")))


def file_matches(path: Path, item: dict) -> bool:
    if not path.is_file() or path.stat().st_size != item["size"]:
        return False
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return digest.casefold() == item["sha256"].casefold()


def verify_downloads(data: dict, directory: Path) -> None:
    validate_manifest(data)
    for item in data["artifacts"]:
        if not file_matches(Path(directory) / item["filename"], item):
            raise ValueError(f"Required source is absent or fails its hash: {item['filename']}")


def check_package_coverage(data: dict, installed: dict[str, str]) -> None:
    available = {}
    for item in data["artifacts"]:
        for name, version in item.get("packages", {}).items():
            available.setdefault(canonical(name), set()).add(version)
    exceptions = {canonical(k): v for k, v in data.get("notice_only_packages", {}).items()}
    for name, version in installed.items():
        key = canonical(name)
        if version in available.get(key, set()):
            continue
        exception = exceptions.get(key, {})
        if key not in COPYLEFT_PACKAGES and exception.get("version") == version and exception.get("reason"):
            continue
        raise ValueError(f"No reviewed exact source for installed {name} {version}")


def installed_packages() -> dict[str, str]:
    if __package__:
        from .collect_licenses import _runtime_closure
    else:
        from collect_licenses import _runtime_closure
    packages = {d.metadata["Name"]: d.version for d in _runtime_closure()}
    # PyInstaller's bootloader is part of the executable, so retain its exact
    # source and special exception even though it is a build dependency.
    packages["pyinstaller"] = distribution("pyinstaller").version
    return packages


def check_installed_file_provenance(data: dict) -> None:
    for check in data.get("runtime_file_checks", []):
        relative = check["relative_path"]
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise ValueError("Unsafe runtime provenance path")
        path = Path(distribution(check["package"]).locate_file(relative))
        digest = hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        if digest != check["sha256_lf"]:
            raise ValueError(f"Installed runtime differs from licensed source: {check['package']}")


def download_artifact(item: dict, directory: Path) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / item["filename"]
    if file_matches(destination, item):
        return destination
    with tempfile.NamedTemporaryFile(dir=directory, suffix=".download", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        request = urllib.request.Request(item["url"], headers={"User-Agent": "PiperReader-source-collector"})
        received = 0
        with urllib.request.urlopen(request, timeout=90) as response, temporary.open("wb") as stream:
            while chunk := response.read(1024 * 1024):
                received += len(chunk)
                if received > item["size"]:
                    raise ValueError(f"Source exceeds its pinned size: {item['filename']}")
                stream.write(chunk)
        if not file_matches(temporary, item):
            raise ValueError(f"Source download failed its size/SHA-256 check: {item['filename']}")
        temporary.replace(destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def write_archive(data: dict, directory: Path, destination: Path, version: str) -> Path:
    verify_downloads(data, directory)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    readme = (
        f"PiperReader {version}: dependency sources\n\n"
        "SOURCES.json records the exact upstream source URLs, revisions, sizes, and SHA-256 hashes.\n"
        "Each source archive retains its upstream license and build instructions.\n"
        "Application source and native-audio build recipes are available beside this asset:\n"
        f"https://github.com/Shettima123/PiperReader/tree/v{version}\n\n"
        "Some permissively licensed dependencies have an explicit notice-only entry; this archive\n"
        "does not claim unavailable source provenance for those entries. Their full bundled notices\n"
        "are distributed in the application's licenses folder. No private user data is included.\n"
    )
    with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".zip", delete=False) as stream:
        temporary = Path(stream.name)
    try:
        # Inputs are already compressed; storing avoids unnecessary recompression.
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("SOURCES.json", json.dumps(data, indent=2) + "\n")
            archive.writestr("README.txt", readme)
            for item in data["artifacts"]:
                archive.write(Path(directory) / item["filename"], arcname=item["filename"])
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--version", default="1.6.5")
    parser.add_argument("--archive-output", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    data = load_manifest(args.manifest)
    check_package_coverage(data, installed_packages())
    check_installed_file_provenance(data)
    if not args.verify_only:
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda item: download_artifact(item, args.output_dir), data["artifacts"]))
    verify_downloads(data, args.output_dir)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "SOURCES.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    destination = args.archive_output or PROJECT_ROOT / "release" / f"PiperReader-{args.version}-dependency-sources.zip"
    write_archive(data, args.output_dir, destination, args.version)
    print(f"Verified {len(data['artifacts'])} source archives: {destination}")


if __name__ == "__main__":
    main()
