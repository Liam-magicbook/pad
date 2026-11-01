#!/usr/bin/env python3
"""回归测试：ChatGPT 附件直传（openai/fileParams）。

覆盖要点：
  1. 工具 schema 严格符合 OpenAI 规范（四个属性、required 恰好两项、带 _meta）；
  2. 文件引用（download_url）能拉回原始字节，SHA-256 与源文件一致；
  3. SSRF 防护：非白名单域名 / http / 内网 IP / 重定向到内网，全部拒绝；
  4. 超限立刻中止（不先下完再判断）；
  5. path 字符串不被当成 base64，给出明确错误；
  6. base64 老路径行为不变。

跑法：
    cd "$(git rev-parse --show-toplevel)" && .venv/bin/python -m unittest tests.test_file_refs -v
"""
import base64
import hashlib
import os
import shutil
import sys
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="pad-file-refs-")
os.environ["PAD_KEY"] = "testkey"
os.environ["PAD_DB"] = str(Path(TMP) / "pad.db")
os.environ["PAD_ARTIFACT_ROOT"] = str(Path(TMP) / "artifacts")
os.environ["PAD_INPUT_FILE_ROOT"] = str(Path(TMP) / "input_files")
# 安全开关是运行时读的（见 server.py 的 _flag/_file_url_hosts），所以测试里
# 用 _Env 精确控制而不是在导入时污染模块状态。

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402

# 一个"像 MP3"的载荷：ID3v2 头 + 假帧数据，足够验证字节级一致
_MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x0a" + bytes(range(256)) * 8
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


@contextmanager
def _Env(**kv):
    """临时改环境变量，退出时还原。"""
    old = {k: os.environ.get(k) for k in kv}
    os.environ.update({k: str(v) for k, v in kv.items()})
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _test_env():
    """打本地桩服务所需的开关（http + 回环豁免 + 白名单）。"""
    return _Env(PAD_FILE_URL_HOSTS="127.0.0.1,localhost",
                PAD_FILE_ALLOW_HTTP="1", PAD_FILE_ALLOW_LOOPBACK="1")


class _Srv:
    """极简 HTTP 服务：按路径返回预设字节，可模拟超大响应与重定向。"""

    def __init__(self):
        self.routes = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                path = self.path.split("?")[0]
                route = outer.routes.get(path)
                if route is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                if route.get("redirect"):
                    self.send_response(302)
                    self.send_header("Location", route["redirect"])
                    self.end_headers()
                    return
                body = route["body"]
                self.send_response(200)
                self.send_header("Content-Type", route.get("ctype", "application/octet-stream"))
                if not route.get("omit_length"):
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()


class FileParamSchemaTest(unittest.TestCase):
    """schema 形状错了 ChatGPT 会静默拒调，所以逐条对照规范验。"""

    @classmethod
    def setUpClass(cls):
        import asyncio
        cls.tools = {t.name: t for t in asyncio.run(server.mcp.list_tools())}

    def _check_file_schema(self, tool_name, param):
        tool = self.tools[tool_name]
        self.assertEqual(tool.meta, {"openai/fileParams": [param]},
                         f"{tool_name} 必须声明 _meta openai/fileParams")
        defs = tool.input_schema["$defs"]
        name, dfn = next(iter(defs.items()))
        props = dfn["properties"]
        # 四个属性必须全部声明
        for p in ("download_url", "file_id", "mime_type", "file_name"):
            self.assertIn(p, props, f"{name}.{p} 未声明")
        # required 必须正好是这两项（多一个就被 Scan Tools 拒收）
        self.assertEqual(sorted(dfn["required"]), ["download_url", "file_id"])
        self.assertFalse(dfn.get("additionalProperties", True))

    def test_probe_tool_schema(self):
        self._check_file_schema("probe_file_upload", "files")

    def test_submit_task_schema(self):
        self._check_file_schema("submit_task", "files")

    def test_submit_task_keeps_base64_compat(self):
        props = self.tools["submit_task"].input_schema["properties"]
        self.assertIn("input_files", props)
        self.assertIn("input_images", props)


class FileFetchTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = _Srv()
        cls.srv.routes = {
            "/a.mp3": {"body": _MP3, "ctype": "audio/mpeg"},
            "/p.png": {"body": _PNG, "ctype": "image/png"},
            "/big.bin": {"body": b"x" * 5000, "ctype": "application/octet-stream"},
            "/no-len.bin": {"body": b"y" * 100, "omit_length": True},
            "/to-evil": {"redirect": "https://evil.example.com/x.mp3"},
            "/to-file": {"redirect": "file:///etc/passwd"},
        }

    @classmethod
    def tearDownClass(cls):
        cls.srv.close()

    def test_mp3_bytes_are_identical(self):
      with _test_env():
        files = server._prepare_input_files([{
            "download_url": self.srv.url("/a.mp3"), "file_id": "file-abc",
            "mime_type": "audio/mpeg", "file_name": "歌.mp3"}])
        self.assertEqual(len(files), 1)
        entry = files[0]
        self.assertEqual(entry["data"], _MP3, "拉回的字节必须与源文件完全一致")
        self.assertEqual(hashlib.sha256(entry["data"]).hexdigest(),
                         hashlib.sha256(_MP3).hexdigest())
        self.assertEqual(entry["name"], "歌.mp3")
        self.assertEqual(entry["mime_type"], "audio/mpeg")

    def test_name_falls_back_to_file_id(self):
      with _test_env():
        files = server._prepare_input_files([{
            "download_url": self.srv.url("/a.mp3"), "file_id": "file-xyz",
            "mime_type": "audio/mpeg"}])
        self.assertTrue(files[0]["name"].startswith("file-xyz"))

    def test_png_from_url_still_gets_magic_check(self):
      with _test_env():
        files = server._prepare_input_files([{
            "download_url": self.srv.url("/p.png"), "file_id": "f1",
            "mime_type": "image/png", "file_name": "p.png"}])
        self.assertEqual(files[0]["mime_type"], "image/png")
        self.assertEqual(files[0]["data"], _PNG)

    def test_lying_image_declaration_rejected(self):
        # 声明 image/png 但远端给的是 mp3 字节 -> 必须拒
      with _test_env():
        with self.assertRaises(ValueError):
            server._prepare_input_files([{
                "download_url": self.srv.url("/a.mp3"), "file_id": "f1",
                "mime_type": "image/png", "file_name": "fake.png"}])

    def test_content_length_over_limit_rejected_before_reading(self):
        orig = server.db.INPUT_FILE_MAX_BYTES
        server.db.INPUT_FILE_MAX_BYTES = 1000
        try:
          with _test_env():
            with self.assertRaises(ValueError) as cm:
                server._prepare_input_files([{
                    "download_url": self.srv.url("/big.bin"), "file_id": "f"}])
            self.assertIn("超过", str(cm.exception))
        finally:
            server.db.INPUT_FILE_MAX_BYTES = orig

    def test_streaming_limit_without_content_length(self):
        # 服务器没给 Content-Length 时，必须靠边读边计数拦住
        orig = server.db.INPUT_FILE_MAX_BYTES
        server.db.INPUT_FILE_MAX_BYTES = 50
        try:
          with _test_env():
            with self.assertRaises(ValueError):
                server._prepare_input_files([{
                    "download_url": self.srv.url("/no-len.bin"), "file_id": "f"}])
        finally:
            server.db.INPUT_FILE_MAX_BYTES = orig

    def test_redirect_to_non_whitelisted_host_blocked(self):
        # 重定向目标必须重新过白名单：302 到外部域也要拦
        with _test_env():
            with self.assertRaises(ValueError) as cm:
                server._prepare_input_files([{
                    "download_url": self.srv.url("/to-evil"), "file_id": "f"}])
            self.assertIn("白名单", str(cm.exception))

    def test_redirect_to_file_scheme_blocked(self):
        with _test_env():
            with self.assertRaises(ValueError) as cm:
                server._prepare_input_files([{
                    "download_url": self.srv.url("/to-file"), "file_id": "f"}])
            self.assertIn("https", str(cm.exception))


class UrlGuardTest(unittest.TestCase):
    """不经网络，纯判 URL 是否放行。"""

    def test_allows_arbitrary_public_host(self):
        # 不限制域名后，正常公网主机应当通过"可拉"判定
        try:
            server._check_url_allowed("https://example.com/x.mp3")
        except server.FileFetchError as exc:
            # 允许"解析失败"（离线环境），但不许因域名被拒
            self.assertNotIn("白名单", str(exc))
            self.assertIn("解析", str(exc))

    def test_rejects_loopback(self):
        with self.assertRaises(server.FileFetchError):
            server._check_url_allowed("http://127.0.0.1/x")

    def test_rejects_metadata_ip(self):
        # 云元数据服务，SSRF 的经典目标
        with self.assertRaises(server.FileFetchError):
            server._check_url_allowed("https://169.254.169.254/latest/meta-data/")

    def test_rejects_missing_host(self):
        with self.assertRaises(server.FileFetchError):
            server._check_url_allowed("https:///nohost")

    def test_default_has_no_domain_restriction(self):
        # 2026-10-26 决策：不搞域名白名单。ChatGPT 附件实测落在 Azure Blob
        # （oaisdmntprwestus3.blob.core.windows.net），写死 oai* 会自己拦自己。
        with _Env(PAD_FILE_URL_HOSTS=None):
            os.environ.pop("PAD_FILE_URL_HOSTS", None)
            self.assertEqual(server._file_url_hosts(), (),
                             "默认必须不限制域名")
        # 任意公网域名都应放行（这里只判策略，不联网）
        self.assertTrue(server._host_allowed("oaisdmntprwestus3.blob.core.windows.net", ()))
        self.assertTrue(server._host_allowed("files.oaiusercontent.com", ()))
        self.assertTrue(server._host_allowed("any-cdn.example.net", ()))

    def test_optional_whitelist_still_works_when_set(self):
        # 需要收紧时 PAD_FILE_URL_HOSTS 仍可用
        with _Env(PAD_FILE_URL_HOSTS="*.openai.com,exact.example.org"):
            hosts = server._file_url_hosts()
            self.assertTrue(server._host_allowed("files.oaiusercontent.com", ()))  # 空格
            self.assertTrue(server._host_allowed("a.openai.com", hosts))
            self.assertFalse(server._host_allowed("evil.com", hosts))
            self.assertTrue(server._host_allowed("exact.example.org", hosts))


class PathRejectionTest(unittest.TestCase):
    def test_local_path_is_not_treated_as_base64(self):
        with self.assertRaises(ValueError) as cm:
            server._prepare_input_files([{"name": "a.mp3", "path": "/mnt/data/a.mp3"}])
        msg = str(cm.exception)
        self.assertIn("path", msg)
        self.assertIn("读不到", msg)

    def test_base64_still_works(self):
        files = server._prepare_input_files([{
            "name": "a.mp3", "mime_type": "audio/mpeg",
            "data_base64": base64.b64encode(_MP3).decode()}])
        self.assertEqual(files[0]["data"], _MP3)

    def test_mixed_sources_in_one_call(self):
        srv = _Srv()
        srv.routes = {"/a.mp3": {"body": _MP3, "ctype": "audio/mpeg"}}
        try:
          with _test_env():
            files = server._prepare_input_files([
                {"download_url": srv.url("/a.mp3"), "file_id": "f1",
                 "file_name": "remote.mp3", "mime_type": "audio/mpeg"},
                {"name": "local.csv", "data_base64": base64.b64encode(b"a,b\n").decode()},
            ])
            self.assertEqual([f["name"] for f in files], ["remote.mp3", "local.csv"])
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
