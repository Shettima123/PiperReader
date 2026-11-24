"""Release materials must fail closed on missing or mismatched source/notices."""

import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile

from build_tools import collect_sources, collect_licenses


def artifact(content=b"source", filename="example.tar.gz"):
    return {
        "id": "example", "component": "example", "version": "1.0",
        "packages": {"example": "1.0"}, "filename": filename,
        "url": "https://example.org/" + filename,
        "size": len(content), "sha256": hashlib.sha256(content).hexdigest(),
    }


def manifest(item):
    return {"schema_version": 1, "artifacts": [item], "notice_only_packages": {}}


class SourceMaterialsTests(unittest.TestCase):
    def test_rejects_unsafe_filename_and_missing_hash(self):
        for filename in ("../source.zip", "x/source.zip", "C:\\source.zip"):
            with self.assertRaises(ValueError):
                collect_sources.validate_manifest(manifest(artifact(filename=filename)))
        item = artifact()
        del item["sha256"]
        with self.assertRaises(ValueError):
            collect_sources.validate_manifest(manifest(item))

    def test_existing_tampered_source_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = artifact()
            (root / item["filename"]).write_bytes(b"altered")
            with self.assertRaises(ValueError):
                collect_sources.verify_downloads(manifest(item), root)

    def test_wrong_download_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = artifact()
            destination = root / item["filename"]
            destination.write_bytes(b"old")
            with mock.patch.object(collect_sources.urllib.request, "urlopen",
                                   return_value=io.BytesIO(b"bad")):
                with self.assertRaises(ValueError):
                    collect_sources.download_artifact(item, root)
            self.assertEqual(destination.read_bytes(), b"old")
            self.assertEqual(list(root.glob("*.download")), [])

    def test_missing_runtime_source_coverage_is_rejected(self):
        with self.assertRaises(ValueError):
            collect_sources.check_package_coverage(manifest(artifact()), {"another": "1.0"})

    def test_wrong_runtime_version_is_rejected(self):
        with self.assertRaises(ValueError):
            collect_sources.check_package_coverage(manifest(artifact()), {"example": "2.0"})

    def test_notice_only_exception_cannot_cover_copyleft_runtime(self):
        data = manifest(artifact())
        data["notice_only_packages"] = {"pymupdf": {"version": "1.0", "reason": "missing"}}
        with self.assertRaises(ValueError):
            collect_sources.check_package_coverage(data, {"pymupdf": "1.0"})

    def test_dependency_zip_contains_only_verified_manifest_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = artifact()
            (root / item["filename"]).write_bytes(b"source")
            (root / "unrelated-secret.json").write_text("private")
            archive = root / "bundle.zip"
            collect_sources.write_archive(manifest(item), root, archive, "1.6.5")
            with zipfile.ZipFile(archive) as result:
                self.assertEqual(set(result.namelist()), {item["filename"], "SOURCES.json", "README.txt"})


class NoticeMaterialsTests(unittest.TestCase):
    def test_copies_full_nested_notices_without_extracting_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.tar.gz"
            with tarfile.open(source, "w:gz") as archive:
                for name, content in (("pkg/LICENSE", b"full license"),
                                      ("pkg/vendor/lib/COPYING.LGPL", b"full LGPL"),
                                      ("../unsafe/LICENSE", b"unsafe"),
                                      ("pkg/README.md", b"readme")):
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
            output = root / "notices"
            output.mkdir()
            copied = collect_licenses.copy_archive_notices(source, output)
            self.assertEqual(len(copied), 2)
            self.assertEqual({p.read_bytes() for p in output.rglob("*") if p.is_file()},
                             {b"full license", b"full LGPL"})
            self.assertFalse((root / "unsafe").exists())

    def test_third_party_and_dash_license_names_are_recognized(self):
        for name in ("ThirdPartyNotices.txt", "LICENSE-MIT", "COPYING.LESSER", "license.terms"):
            self.assertTrue(collect_licenses.LICENSE_NAMES.match(name), name)

    def test_missing_notice_fails_instead_of_successful_index(self):
        with self.assertRaises(RuntimeError):
            collect_licenses.require_notice("unlicensed-example", [])


if __name__ == "__main__":
    unittest.main()
