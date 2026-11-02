#!/usr/bin/env python3
"""Regression test: get_artifact dispatches by MIME — image artifacts must come back as a
native ImageContent rather than decoding the binary into text.

Covers the real incident from 2026-09-24 (t-20260924-f408c1 / profile_NASA.png): the MCP
returned image/png as text content, so the caller received binary text starting with
garbage and could not read it visually.

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

TMP = tempfile.mkdtemp(prefix="pad-artifact-mime-")
os.environ["PAD_KEY"] = "testkey"
os.environ["PAD_DB"] = str(Path(TMP) / "pad.db")
os.environ["PAD_ARTIFACT_ROOT"] = str(Path(TMP) / "artifacts")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402
from mcp.server.mcpserver import Image  # noqa: E402

# A real 1x1 PNG (valid PNG header + IDAT)
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
_TEXT = b"line01 alpha\nline02 beta\nline03 gamma\n"


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


class ArtifactMimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import db
        db.init()
        cls.task_id = db.submit_task("test.cap", "mime dispatch regression")["task_id"]
        db.save_artifact(cls.task_id, "test-node", "pic.png", _PNG)
        db.save_artifact(cls.task_id, "test-node", "notes.txt", _TEXT)

    def test_image_format_sniffs_magic(self):
        self.assertEqual(server._image_format("a.png", b"\x89PNG\r\n\x1a\n...."), "png")
        self.assertEqual(server._image_format("a.jpg", b"\xff\xd8\xff\xe0...."), "jpeg")
        self.assertEqual(server._image_format("a.gif", b"GIF89a...."), "gif")
        self.assertEqual(server._image_format("a.webp", b"RIFF\x00\x00\x00\x00WEBPVP8 "), "webp")
        self.assertIsNone(server._image_format("notes.txt", _TEXT))

    def test_get_artifact_image_returns_image_content(self):
        out = server.get_artifact(self.task_id, "pic.png")
        self.assertIsInstance(out, Image)
        block = out.to_image_content()
        self.assertEqual(block.type, "image")
        self.assertEqual(block.mime_type, "image/png")
        self.assertEqual(base64.b64decode(block.data), _PNG)

    def test_get_artifact_text_keeps_pagination(self):
        out = server.get_artifact(self.task_id, "notes.txt", offset=6, limit=11)
        self.assertIsInstance(out, dict)
        self.assertEqual(out["offset"], 6)
        self.assertEqual(out["returned"], 11)
        self.assertEqual(out["next_offset"], 17)
        self.assertEqual(out["content"], _TEXT[6:17].decode())

    def test_get_task_image_chunk_is_hint_not_binary(self):
        chunk = server._artifact_chunk(self.task_id, "pic.png", 0, 8000)
        self.assertEqual(chunk.get("mime"), "image")
        self.assertNotIn("content", chunk)
        self.assertIn("get_artifact", chunk["note"])

    def test_missing_artifact_unchanged(self):
        out = server.get_artifact(self.task_id, "nope.bin")
        self.assertEqual(out["error"], "not_found")


if __name__ == "__main__":
    unittest.main()
