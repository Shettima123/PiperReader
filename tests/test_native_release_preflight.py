import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("native_preflight", ROOT / "build_tools/prepare_native_audio.py")
MODULE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(MODULE)


class NativeReleasePreflightTests(unittest.TestCase):
    def fixture(self, root):
        dll = root / "libsndfile_x64.dll"
        dll.write_bytes(b"native library fixture")
        manifest = {"library": "libsndfile 1.2.2", "architecture": "Windows x64",
                    "dll_sha256": hashlib.sha256(dll.read_bytes()).hexdigest()}
        (root / "build-manifest.json").write_text(json.dumps(manifest))
        sources = json.loads((ROOT / "build_tools/native_audio/sources.json").read_text())
        (root / "sources.json").write_text(json.dumps(sources))
        (root / "verification.json").write_text(json.dumps({
            "passed": True, "dll_sha256": manifest["dll_sha256"]}))
        return dll

    def test_accepts_exact_verified_native_build(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dll = self.fixture(root)
            self.assertEqual(MODULE.validate_native_build(root), dll)

    def test_rejects_changed_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root).write_bytes(b"different binary")
            with self.assertRaisesRegex(ValueError, "checksum"):
                MODULE.validate_native_build(root)

    def test_rejects_unverified_library(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            (root / "verification.json").write_text('{"passed": false}')
            with self.assertRaisesRegex(ValueError, "verification"):
                MODULE.validate_native_build(root)

    def test_rejects_different_source_recipe(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            (root / "sources.json").write_text('{}')
            with self.assertRaisesRegex(ValueError, "source"):
                MODULE.validate_native_build(root)
