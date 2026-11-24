"""Collect license texts for Python and the bundled runtime distributions."""

from __future__ import annotations

import re
import shutil
import sys
from collections import deque
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

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
    r"^(license|licence|copying|notice|authors?|copyright)(\..*)?$",
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
        except PackageNotFoundError:
            print(f"WARNING: installed distribution not found: {requested_name}")
            continue
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


def collect() -> Path:
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

    python_dir = OUTPUT_ROOT / f"Python-{sys.version_info.major}.{sys.version_info.minor}"
    python_dir.mkdir()
    python_used: set[str] = set()
    python_candidates = [
        Path(sys.base_prefix) / "LICENSE.txt",
        Path(sys.base_prefix) / "tcl" / "tcl8.6" / "license.terms",
        Path(sys.base_prefix) / "tcl" / "tk8.6" / "license.terms",
    ]
    copied_python = []
    for candidate in python_candidates:
        if candidate.is_file():
            copied_python.append(
                _copy_unique(candidate, python_dir, python_used)
            )
    index_lines.append(
        f"Python {sys.version.split()[0]}: "
        + (", ".join(copied_python) if copied_python else "license text not found")
    )

    for dist in _runtime_closure():
        name = dist.metadata.get("Name", "unknown")
        version = dist.version
        destination = OUTPUT_ROOT / f"{_safe_name(name)}-{_safe_name(version)}"
        destination.mkdir()
        used: set[str] = set()
        copied = [
            _copy_unique(source, destination, used)
            for source in _license_files(dist)
        ]
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
            + (", ".join(copied) if copied else "license text not found in wheel")
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
    return index_path


if __name__ == "__main__":
    print(collect())
