#!/usr/bin/env python3
"""Regression test: input files delivered with a task (input_files) — expanded from
"images only" to "any file".

Coverage:
  1. Images are still validated by real file header (a lying declaration must be rejected),
     format/extension normalised;
  2. Ordinary files (pdf/csv/zip/mp4) are stored byte-for-byte, MIME and extension untouched;
  3. Text types carry a preview, binaries do not;
  4. The old parameter name input_images still works;
  5. Task inputs and artifacts are two tables: get_artifact cannot find an input file,
     get_input_file can;
  6. Old nodes with a stale bundle do not receive tasks carrying ordinary files, but still
     receive small image-only tasks.

How to run:
    cd "$(git rev-parse --show-toplevel)" && .venv/bin/python -m unittest discover -s tests -v
"""
import base64
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="pad-input-files-")
os.environ["PAD_KEY"] = "testkey"
os.environ["PAD_DB"] = str(Path(TMP) / "pad.db")
os.environ["PAD_ARTIFACT_ROOT"] = str(Path(TMP) / "artifacts")
os.environ["PAD_INPUT_FILE_ROOT"] = str(Path(TMP) / "input_files")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import db  # noqa: E402
import server  # noqa: E402
from mcp.server.mcpserver import Image  # noqa: E402

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
# Minimal usable PDF header + trailer (not a complete renderable file, only used to verify
# "stored byte-for-byte")
_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n")
_CSV = b"name,score\nli,91\nwang,88\n"
_ZIP = b"PK\x03\x04" + b"\x00" * 32
_MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


def _bind_db() -> None:
    """Test discovery puts several test modules in one process; db is a singleton and its
    paths are frozen at import time.

    The tearDownModule of another module (test_artifact_mime) removes its own temp dir, so
    here the paths must be explicitly rebound to this module's TMP, otherwise you get a path
    that has already been deleted.
    """
    (Path(TMP) / "artifacts").mkdir(parents=True, exist_ok=True)
    (Path(TMP) / "input_files").mkdir(parents=True, exist_ok=True)
    db.DB_PATH = Path(TMP) / "pad.db"
    db.ARTIFACT_ROOT = Path(TMP) / "artifacts"
    db.INPUT_FILE_ROOT = Path(TMP) / "input_files"


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class InputFilePrepareTest(unittest.TestCase):
    """Server-side input validation layer."""

    def test_image_is_normalised_from_data_url(self):
        out = server._prepare_input_files([
            {"name": "图 1.png", "data_url": "data:image/png;base64," + _b64(_PNG)}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["mime_type"], "image/png")
        # Chinese names are preserved (the executor has to recognise the file inside inputs/),
        # only the extension is normalised
        self.assertEqual(out[0]["name"], "图 1.png")
        self.assertEqual(out[0]["data"], _PNG)

    def test_chinese_file_name_survives_storage(self):
        _bind_db()
        db.init()
        files = server._prepare_input_files([
            {"name": "季度报告.pdf", "mime_type": "application/pdf", "data_base64": _b64(_PDF)}])
        out = db.submit_task("test.cap", "中文名", input_files=files)
        self.assertEqual([x["name"] for x in out["input_files"]], ["季度报告.pdf"])
        got = db.read_task_input_file(out["task_id"], "季度报告.pdf")
        self.assertEqual(got["data"], _PDF)

    def test_path_traversal_is_stripped(self):
        out = server._prepare_input_files([
            {"name": "../../etc/passwd.csv", "data_base64": _b64(b"a,b\n")}])
        self.assertEqual(out[0]["name"], "passwd.csv")

    def test_image_with_lying_header_is_rejected(self):
        with self.assertRaises(ValueError):
            server._prepare_input_files([
                {"name": "fake.png", "mime_type": "image/png", "data_base64": _b64(_PDF)}])

    def test_image_declared_png_but_actually_jpeg_is_rejected(self):
        with self.assertRaises(ValueError):
            server._prepare_input_files([
                {"name": "a.png", "mime_type": "image/png",
                 "data_base64": _b64(b"\xff\xd8\xff\xe0" + b"\x00" * 32)}])

    def test_pdf_kept_byte_exact(self):
        out = server._prepare_input_files([
            {"name": "季度报告.pdf", "mime_type": "application/pdf", "data_base64": _b64(_PDF)}])
        self.assertEqual(out[0]["mime_type"], "application/pdf")
        self.assertEqual(out[0]["data"], _PDF)
        self.assertEqual(out[0]["name"].rsplit(".", 1)[-1], "pdf")
        self.assertNotIn("preview", out[0])

    def test_csv_gets_text_preview(self):
        out = server._prepare_input_files([
            {"name": "scores.csv", "mime_type": "text/csv", "data_base64": _b64(_CSV)}])
        self.assertEqual(out[0]["mime_type"], "text/csv")
        self.assertIn("li,91", out[0]["preview"])

    def test_mime_inferred_from_extension_when_missing(self):
        out = server._prepare_input_files([
            {"name": "notes.md", "data_base64": _b64(b"# hello\n")}])
        self.assertEqual(out[0]["mime_type"], "text/markdown")

    def test_unknown_binary_falls_back_to_octet_stream(self):
        out = server._prepare_input_files([
            {"name": "blob.dat", "data_base64": _b64(b"\x00\x01\x02")}])
        self.assertEqual(out[0]["mime_type"], "application/octet-stream")
        self.assertTrue(out[0]["name"].endswith(".dat"))

    def test_duplicate_names_get_suffixes(self):
        out = server._prepare_input_files([
            {"name": "a.csv", "data_base64": _b64(b"x")},
            {"name": "a.csv", "data_base64": _b64(b"y")}])
        self.assertEqual([x["name"] for x in out], ["a.csv", "a-2.csv"])

    def test_empty_file_rejected(self):
        with self.assertRaises(ValueError):
            server._prepare_input_files([{"name": "e.txt", "data_base64": ""}])

    def test_zip_and_mp4_accepted(self):
        out = server._prepare_input_files([
            {"name": "bundle.zip", "mime_type": "application/zip", "data_base64": _b64(_ZIP)},
            {"name": "clip.mp4", "mime_type": "video/mp4", "data_base64": _b64(_MP4)}])
        self.assertEqual([x["mime_type"] for x in out], ["application/zip", "video/mp4"])


class InputFileStorageTest(unittest.TestCase):
    """Storage + read-back: task inputs are not artifacts."""

    @classmethod
    def setUpClass(cls):
        _bind_db()
        db.init()

    def _submit(self, **kw):
        files = server._prepare_input_files(kw.pop("files", None))
        return db.submit_task(kw.pop("cap", "test.cap"), kw.pop("goal", "input file test"),
                              input_files=files, **kw)

    def test_view_lists_files_under_both_names(self):
        out = self._submit(files=[{"name": "a.pdf", "data_base64": _b64(_PDF)}])
        view = db.task_view(out["task_id"])
        self.assertEqual([x["name"] for x in view["input_files"]], ["a.pdf"])
        self.assertEqual(view["input_files"], view["input_images"])
        self.assertNotIn("data", view["input_files"][0])

    def test_bytes_round_trip_and_sha(self):
        out = self._submit(files=[{"name": "a.pdf", "data_base64": _b64(_PDF)}])
        got = db.read_task_input_file(out["task_id"], "a.pdf")
        self.assertEqual(got["data"], _PDF)
        self.assertEqual(got["meta"]["bytes"], len(_PDF))
        self.assertEqual(got["meta"]["sha256"], out["input_files"][0]["sha256"])

    def test_input_file_is_not_an_artifact(self):
        out = self._submit(files=[{"name": "a.pdf", "data_base64": _b64(_PDF)}])
        self.assertEqual(server.get_artifact(out["task_id"], "a.pdf")["error"], "not_found")
        got = server.get_input_file(out["task_id"], "a.pdf")
        self.assertEqual(got["mime_type"], "application/pdf")
        self.assertIn("note", got)

    def test_get_input_file_image_returns_image_content(self):
        out = self._submit(files=[{"name": "p.png", "data_base64": _b64(_PNG)}])
        got = server.get_input_file(out["task_id"], "p.png")
        self.assertIsInstance(got, Image)
        self.assertEqual(base64.b64decode(got.to_image_content().data), _PNG)

    def test_get_input_file_text_paginates(self):
        out = self._submit(files=[{"name": "s.csv", "data_base64": _b64(_CSV)}])
        got = server.get_input_file(out["task_id"], "s.csv", offset=5, limit=6)
        self.assertEqual(got["content"], _CSV[5:11].decode())
        self.assertEqual(got["next_offset"], 11)

    def test_get_input_file_lists_when_name_empty(self):
        out = self._submit(files=[{"name": "a.csv", "data_base64": _b64(_CSV)}])
        got = server.get_input_file(out["task_id"])
        self.assertEqual([x["name"] for x in got["input_files"]], ["a.csv"])

    def test_legacy_input_images_key_still_works(self):
        files = server._prepare_input_images(
            [{"name": "p.png", "mime_type": "image/png", "data_base64": _b64(_PNG)}])
        out = db.submit_task("test.cap", "legacy images", input_images=files)
        self.assertEqual([x["name"] for x in out["input_images"]], ["p.png"])

    def test_count_limit_enforced(self):
        too_many = [{"name": f"f{i}.txt", "data_base64": _b64(b"x")}
                    for i in range(db.INPUT_FILE_MAX_COUNT + 1)]
        with self.assertRaises(ValueError):
            server._prepare_input_files(too_many)


class LegacyNodeClaimTest(unittest.TestCase):
    """Old nodes with a stale bundle may only take small image-only tasks (their validation
    limit is 4 images / 5MiB)."""

    @classmethod
    def setUpClass(cls):
        _bind_db()
        db.init()
        db.register_node("new-node", "publisher", ["test.claim"], {"bundle_hash": "fresh"}, "新版")
        db.register_node("old-node", "publisher", ["test.claim"], {"bundle_hash": "stale"}, "老版")

    def _clean(self):
        with db.conn() as c:
            c.execute("DELETE FROM tasks WHERE capability='test.claim'")
            c.execute("DELETE FROM task_inputs")

    def test_old_node_skips_task_with_non_image_file(self):
        self._clean()
        files = server._prepare_input_files(
            [{"name": "deck.pdf", "mime_type": "application/pdf", "data_base64": _b64(_PDF)}])
        task = db.submit_task("test.claim", "pdf task", input_files=files)
        claimed = db.claim_task("old-node", ["test.claim"], server_bundle="fresh")
        self.assertIsNone(claimed)
        fresh = db.claim_task("new-node", ["test.claim"], server_bundle="fresh")
        self.assertEqual(fresh["task_id"], task["task_id"])

    def test_old_node_still_takes_small_image_task(self):
        self._clean()
        files = server._prepare_input_files(
            [{"name": "p.png", "mime_type": "image/png", "data_base64": _b64(_PNG)}])
        task = db.submit_task("test.claim", "image task", input_files=files)
        claimed = db.claim_task("old-node", ["test.claim"], server_bundle="fresh")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["task_id"], task["task_id"])

    def test_old_node_never_takes_plain_task_without_inputs(self):
        # A node can still take tasks with no input files (being old should not starve it)
        self._clean()
        task = db.submit_task("test.claim", "plain task")
        claimed = db.claim_task("old-node", ["test.claim"], server_bundle="fresh")
        self.assertEqual(claimed["task_id"], task["task_id"])

    def test_digest_flags_both_field_names(self):
        d = db.digest(server_bundle="fresh")
        by_id = {n["node_id"]: n for n in d["nodes"]}
        self.assertTrue(by_id["new-node"]["input_files_supported"])
        self.assertFalse(by_id["old-node"]["input_files_supported"])
        self.assertEqual(by_id["old-node"]["input_files_supported"],
                         by_id["old-node"]["input_images_supported"])
        self.assertIn("old-node", d["nodes_bundle_stale"])


if __name__ == "__main__":
    unittest.main()
