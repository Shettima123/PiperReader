"""Offline preflight for the exact upstream Edge speech protocol fix.

This runs only during development/builds. It does not synthesize speech, inspect
user data, or contact Microsoft. Version metadata alone cannot distinguish the
published 7.2.8 client from the pinned upstream revision, which also says 7.2.8.
"""

from __future__ import annotations

import hashlib
import importlib
import sys
from pathlib import Path


EDGE_TTS_REVISION = "ba61d94442c18893f0c4d3c5ad253718b21ab93e"
EXPECTED_SOURCE_HASHES = {
    "edge_tts.constants":
        "6c987fafa0a10aad81d8d5fc3447a14f2bca2eb86b4f97296c384253d3906c94",
    "edge_tts.drm":
        "a5fde415ddb8eebd133261c789eac03efbb34cd74cb41b8cb18323b72f0b0062",
}
INSTALL_HINT = (
    "Install the pinned dependency using this build's Python: "
    "python -m pip install --force-reinstall --no-deps -r requirements-online.txt"
)


def check_online_dependencies() -> None:
    """Reject old or locally altered protocol code before packaging it."""
    modules = {
        name: importlib.import_module(name) for name in EXPECTED_SOURCE_HASHES
    }
    if modules["edge_tts.constants"].SEC_MS_GEC_VERSION != "2-143.0.3650.75":
        raise ValueError("The Microsoft speech dependency lacks the pinned v2 protocol.")
    for name, expected_hash in EXPECTED_SOURCE_HASHES.items():
        source_path = Path(modules[name].__file__)
        if hashlib.sha256(source_path.read_bytes()).hexdigest() != expected_hash:
            raise ValueError(f"{name} does not match upstream revision {EDGE_TTS_REVISION}.")


def main() -> int:
    try:
        check_online_dependencies()
    except (ImportError, OSError, AttributeError, TypeError, ValueError) as exc:
        print(f"Microsoft speech dependency check failed: {exc}", file=sys.stderr)
        print(INSTALL_HINT, file=sys.stderr)
        return 1
    print(f"Microsoft speech dependency verified offline: {EDGE_TTS_REVISION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
