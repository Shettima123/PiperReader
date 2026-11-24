"""Collect license texts for Python and the bundled runtime distributions."""

from __future__ import annotations

import re
import shutil
import sys
import argparse
import hashlib
import json
import tarfile
import zipfile
from collections import deque
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path, PurePosixPath

from packaging.requirements import Requirement


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_ROOT = PROJECT_ROOT / "build_assets" / "licenses"
TESSDATA_FAST_REVISION = "87416418657359cb625c412a48b6e1d6d41c29bd"
EXTERNAL_LICENSES = (
    (
        "tessdata_fast",
        TESSDATA_FAST_REVISION,
        "Apache-2.0",
        PROJECT_ROOT / "build_assets" / "ocr" / "LICENSE",
    ),
)
SEED_DISTRIBUTIONS = (
    "piper-tts",
    "kokoro-onnx",
    "onnxruntime",
    "espeakng-loader",
    "phonemizer-fork",
    "numpy",
    "pywin32",
    "pystray",
    "pillow",
    "pymupdf",
    "pypdf",
    "lxml",
    "edge-tts",
    "soundfile",
    "aiohttp",
    "certifi",
)
LICENSE_NAMES = re.compile(
    r"^(license|licence|copying|notice|authors?|copyright|third.?party.?notices?)([._-].*)?$",
    re.IGNORECASE,
)


def _safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "item"


def _runtime_closure() -> list:
    """Resolve installed, non-extra runtime requirements from the seed set."""

    pending = deque(SEED_DISTRIBUTIONS)
    found: dict[str, object] = {}
    while pending:
        requested_name = pending.popleft()
        key = re.sub(r"[-_.]+", "-", requested_name).lower()
        if key in found:
            continue
        try:
            dist = distribution(requested_name)
        except PackageNotFoundError as exc:
            raise RuntimeError(f"Required runtime distribution is missing: {requested_name}") from exc
        canonical = re.sub(
            r"[-_.]+", "-", dist.metadata.get("Name", requested_name)
        ).lower()
        if canonical in found:
            continue
        found[canonical] = dist
        for requirement_text in dist.requires or ():
            requirement = Requirement(requirement_text)
            if requirement.marker and not requirement.marker.evaluate({"extra": ""}):
                continue
            pending.append(requirement.name)
    return [found[name] for name in sorted(found)]


def _license_files(dist) -> list[Path]:
    candidates: list[Path] = []
    for relative in dist.files or ():
        if LICENSE_NAMES.match(Path(str(relative)).name):
            candidate = Path(dist.locate_file(relative)).resolve()
            if candidate.is_file() and candidate not in candidates:
                candidates.append(candidate)
    return candidates


def _copy_unique(source: Path, destination_dir: Path, used: set[str]) -> str:
    base = _safe_name(source.name)
    candidate = base
    counter = 2
    while candidate.casefold() in used:
        candidate = f"{Path(base).stem}_{counter}{Path(base).suffix}"
        counter += 1
    used.add(candidate.casefold())
    shutil.copy2(source, destination_dir / candidate)
    return candidate


def require_notice(name: str, files: list) -> None:
    if not files:
        raise RuntimeError(f"Required license/notice text is missing for {name}")


def copy_archive_notices(source: Path, destination: Path) -> list[str]:
    """Copy complete notice bytes without extracting arbitrary archive paths."""
    destination.mkdir(parents=True, exist_ok=True)
    copied = []
    total = 0

    def copy_member(name, size, read):
        nonlocal total
        parts = PurePosixPath(name).parts
        if not parts or name.startswith("/") or ".." in parts or "\\" in name or ":" in name:
            return
        if not LICENSE_NAMES.match(parts[-1]):
            return
        if size > 16 * 1024 * 1024 or total + size > 128 * 1024 * 1024:
            raise RuntimeError(f"Unexpectedly large source notice in {source.name}: {name}")
        content = read()
        if len(content) != size:
            raise RuntimeError(f"Truncated source notice: {name}")
        total += size
        # Flatten names to avoid path traversal and long Windows source paths.
        relative = "__".join(parts[1:] or parts)
        filename = _safe_name(relative)
        if len(filename) > 150:
            filename = filename[:130] + "_" + hashlib.sha256(name.encode()).hexdigest()[:12]
        candidate = destination / filename
        if candidate.exists() and candidate.read_bytes() != content:
            candidate = destination / (hashlib.sha256(name.encode()).hexdigest()[:12] + "_" + filename)
        candidate.write_bytes(content)
        copied.append(candidate.name)

    if zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            for member in archive.infolist():
                if not member.is_dir():
                    copy_member(member.filename, member.file_size, lambda m=member: archive.read(m))
    else:
        with tarfile.open(source, "r:*") as archive:
            for member in archive:
                if member.isfile():
                    copy_member(member.name, member.size, lambda m=member: archive.extractfile(m).read())
    return copied


def collect(sources_manifest: Path | None = None) -> Path:
    if __package__:
        from . import collect_sources
    else:
        import collect_sources

    sources_manifest = sources_manifest or PROJECT_ROOT / "build_assets" / "sources" / "SOURCES.json"
    source_data = collect_sources.load_manifest(sources_manifest)
    collect_sources.verify_downloads(source_data, sources_manifest.parent)
    runtime = _runtime_closure()
    collect_sources.check_package_coverage(source_data, {
        d.metadata["Name"]: d.version for d in runtime
    })
    output_resolved = OUTPUT_ROOT.resolve()
    expected_parent = (PROJECT_ROOT / "build_assets").resolve()
    if output_resolved.parent != expected_parent or output_resolved.name != "licenses":
        raise RuntimeError(f"Refusing to replace unexpected path: {output_resolved}")
    if OUTPUT_ROOT.exists():
        shutil.rmtree(OUTPUT_ROOT)
    OUTPUT_ROOT.mkdir(parents=True)

    index_lines = [
        "Piper Reader bundled third-party license index",
        "Generated from the Python environment used to build this package.",
        "",
    ]

    source_notices = {}
    source_packages = {}
    for item in source_data["artifacts"]:
        destination = OUTPUT_ROOT / "source-notices" / _safe_name(item["id"])
        copied = copy_archive_notices(sources_manifest.parent / item["filename"], destination)
        source_notices[item["id"]] = copied
        for package in item.get("packages", {}):
            source_packages.setdefault(collect_sources.canonical(package), []).extend(copied)
        index_lines.append(f"Source {item['component']} {item['version']} | {len(copied)} complete source notices | source-notices/{_safe_name(item['id'])}")

    supplemental = PROJECT_ROOT / "licenses"
    if not supplemental.is_dir():
        raise RuntimeError("Required checked-in supplemental licenses directory is missing")
    shutil.copytree(supplemental, OUTPUT_ROOT / "supplemental")
    application_license = PROJECT_ROOT / "LICENSE"
    require_notice("PiperReader AGPL-3.0", [application_license] if application_license.is_file() else [])
    shutil.copy2(application_license, OUTPUT_ROOT / "PiperReader-AGPL-3.0.txt")

    python_dir = OUTPUT_ROOT / f"Python-{sys.version_info.major}.{sys.version_info.minor}"
    python_dir.mkdir()
    python_used: set[str] = set()
    python_candidates = [
        Path(sys.base_prefix) / "LICENSE.txt",
        PROJECT_ROOT / "licenses" / "tcl" / "license.terms",
        PROJECT_ROOT / "licenses" / "tk" / "license.terms",
    ]
    copied_python = []
    for candidate in python_candidates:
        if candidate.is_file():
            copied_python.append(
                _copy_unique(candidate, python_dir, python_used)
            )
        else:
            raise RuntimeError(f"Required Python/Tcl/Tk license is missing: {candidate.name}")
    index_lines.append(
        f"Python {sys.version.split()[0]}: "
        + (", ".join(copied_python) if copied_python else "license text not found")
    )

    for dist in runtime:
        name = dist.metadata.get("Name", "unknown")
        version = dist.version
        destination = OUTPUT_ROOT / f"{_safe_name(name)}-{_safe_name(version)}"
        destination.mkdir()
        used: set[str] = set()
        copied = [
            _copy_unique(source, destination, used)
            for source in _license_files(dist)
        ]
        extra_files = list((supplemental / collect_sources.canonical(name)).rglob("*"))
        extra_notices = [p for p in extra_files if p.is_file() and LICENSE_NAMES.match(p.name)]
        available_notices = copied + source_packages.get(collect_sources.canonical(name), []) + extra_notices
        require_notice(f"{name} {version}", available_notices)
        raw_expression = (
            dist.metadata.get("License-Expression")
            or dist.metadata.get("License")
            or "not declared in package metadata"
        )
        raw_expression = " ".join(raw_expression.split())
        expression = raw_expression
        if len(raw_expression) > 240:
            metadata_license = "METADATA-LICENSE.txt"
            (destination / metadata_license).write_text(
                raw_expression + "\n", encoding="utf-8"
            )
            copied.append(metadata_license)
            expression = raw_expression[:237] + "..."
        index_lines.append(
            f"{name} {version} | {expression} | "
            + (", ".join(copied) if copied else "complete notices in source-notices and/or supplemental")
        )

    for name, version, expression, source in EXTERNAL_LICENSES:
        if not source.is_file():
            raise RuntimeError(f"Required external license was not found: {source}")
        destination = OUTPUT_ROOT / f"{_safe_name(name)}-{_safe_name(version)}"
        destination.mkdir()
        copied = _copy_unique(source, destination, set())
        index_lines.append(f"{name} {version} | {expression} | {copied}")

    index_path = OUTPUT_ROOT / "INDEX.txt"
    index_path.write_text("\n".join(index_lines) + "\n", encoding="utf-8")
    shutil.copy2(sources_manifest, OUTPUT_ROOT / "SOURCES.json")
    (OUTPUT_ROOT / "SOURCE_ACCESS.txt").write_text(
        "PiperReader application and dependency source access\n\n"
        "Application source, native audio build recipes, and the dependency source archive\n"
        "are available together at https://github.com/Shettima123/PiperReader/releases/tag/v1.6.5\n"
        "Dependency archive: PiperReader-1.6.5-dependency-sources.zip\n"
        "SOURCES.json records exact upstream revisions and artifact hashes.\n"
        "The source archive preserves each upstream license and build instructions.\n"
        "See supplemental and source-notices for the complete applicable notices, including\n"
        "the LGPL audio components, eSpeak NG GPL, MuPDF AGPL, and PyInstaller exception.\n",
        encoding="utf-8",
    )
    return index_path


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources-manifest", type=Path)
    arguments = parser.parse_args()
    print(collect(arguments.sources_manifest))
