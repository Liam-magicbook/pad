"""PAD server: one process, SQLite state, two doors.

  /pad/<KEY>/mcp            MCP (for the brain / ChatGPT)
  /pad/<KEY>/api/...        REST (for the LD agent on each machine)

The brain only ever sees 9 task tools. It has no shell, no machine id, no repo.
"""
import base64
import binascii
import hashlib
import io
import ipaddress
import json
import mimetypes
import os
import socket
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from mcp.server.mcpserver import MCPServer, Image
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import (
    CallToolResult,
    ContentBlock,
    EmbeddedResource,
    ImageContent,
    ResourceLink,
    TextContent,
)
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

import bundle
import db

HERE = Path(__file__).resolve().parent
SERVER_BUNDLE = bundle.bundle_hash(HERE)
PAD_KEY = os.environ["PAD_KEY"]
PAD_DB = os.environ.get("PAD_DB", "pad.db")
ALLOWED_HOSTS = [h.strip() for h in os.environ.get(
    "PAD_ALLOWED_HOSTS", "localhost,127.0.0.1"
).split(",") if h.strip()]
# The MCP DNS-rebinding allowlist matches the Host header **literally**, and that header
# carries a port. Listing only `127.0.0.1` makes `127.0.0.1:8790` an Invalid Host → 421 (which
# looks a lot like "the service is down"). So entries without a port get a `:*` wildcard.
ALLOWED_HOSTS += [f"{h}:*" for h in list(ALLOWED_HOSTS) if ":" not in h]

# The artifacts human-delivery plane is not inside PAD: PAD is the thinnest proxy, forwarding
# get_artifact_delivery(task_id, name) to the same machine's Artifact Bridge (an internal call;
# the key never crosses MCP and is never returned to ChatGPT). The Bridge validates registration,
# lazily materializes and signs it. Likewise get_artifact_observe only forwards to the Bridge's
# observe plane — decoding / frame extraction / safety all live there, PAD duplicates nothing.
AB_DELIVERY_URL = os.environ.get("AB_DELIVERY_URL", "http://127.0.0.1:8791/delivery")
AB_DELIVERY_KEY = os.environ.get("AB_DELIVERY_KEY", "")


def _derive_observe_url(delivery_url: str) -> str:
    """observe and delivery are one service on one machine; derive the sibling from delivery."""
    base = (delivery_url or "").rstrip("/")
    if base.endswith("/delivery"):
        return base[: -len("/delivery")] + "/observe"
    return f"{base}/observe"


AB_OBSERVE_URL = (os.environ.get("AB_OBSERVE_URL", "").strip()
                  or _derive_observe_url(AB_DELIVERY_URL))

# The Bridge's observe returns a content array written in the MCP wire format (camelCase);
# we restore it into native content block objects here (especially the ImageContent for
# video) and must never hand it back to the model as JSON text.
_CONTENT_BLOCK_ADAPTER = TypeAdapter(ContentBlock)


def _to_content_blocks(blocks: list) -> list:
    out: list = []
    for b in blocks:
        try:
            out.append(_CONTENT_BLOCK_ADAPTER.validate_python(b))
        except Exception:
            # One unparseable block must not swallow the call; degrade to text, keep the info.
            out.append(TextContent(type="text", text=json.dumps(b, ensure_ascii=False)[:2000]))
    return out

db.DB_PATH = db.Path(PAD_DB)

mcp = MCPServer(
    name="PAD",
    instructions=(
        "PAD 是任务的持久化中枢。你只能通过它下发目标、查看进度和最终报告。"
        "你不能执行命令、访问代码、指挥具体机器。"
        "下发请用 capability（例如 publish.wechat），不要指定机器。"
        "submit_task 支持 input_files，以 base64 或 data URL 随任务把任意文件（图片/PDF/"
        "Office/CSV/压缩包/音视频）交给节点；get_input_file 可回读。"
    ),
)


# ------------------------------------------------ artifact MIME branching
# Artifacts store bytes only, no MIME, so images are detected header-first, extension as fallback.
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)
_RASTER_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}


class OpenAIFileParam(BaseModel):
    """ChatGPT's openai/fileParams file reference.

    The shape is pinned by the OpenAI spec, it is not arbitrary:
      - all four properties must be declared (mime_type / file_name may be non-required);
      - required must be **exactly** ["download_url", "file_id"];
      - one extra required property, or an optional one marked required, and Scan Tools rejects it.

    So extra="forbid" plus explicit Optional keeps the generated JSON Schema on spec.
    """
    model_config = ConfigDict(extra="forbid")

    download_url: str = Field(
        description="临时下载地址，仅在本次工具调用期间有效；服务端会立刻取回字节。")
    file_id: str = Field(description="ChatGPT 侧的文件 ID，仅作标识/兜底文件名。")
    mime_type: Optional[str] = Field(
        default=None, description="文件 MIME（可能为空，服务端会按内容复核）。")
    file_name: Optional[str] = Field(
        default=None, description="原始文件名，用于保留扩展名与可读名。")


def _sniff_image(data: bytes) -> str | None:
    """Sniff the image format from the real file header only. Input side: no extension fallback."""
    head = bytes(data[:16])
    for magic, fmt in _IMAGE_MAGIC:
        if head.startswith(magic):
            return fmt
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def _image_format(name: str, data: bytes) -> str | None:
    """Returns the format Image() wants (png/jpeg/gif/webp) on a hit, otherwise None."""
    head = bytes(data[:16])
    for magic, fmt in _IMAGE_MAGIC:
        if head.startswith(magic):
            return fmt
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    mime = mimetypes.guess_type(str(name))[0] or ""
    ext = mime.split("/", 1)[1].split("+")[0] if mime.startswith("image/") else ""
    if ext in _RASTER_EXTS:
        return "jpeg" if ext == "jpg" else ext
    return None


_IMAGE_MIME = {"png": "image/png", "jpeg": "image/jpeg",
               "gif": "image/gif", "webp": "image/webp"}
_IMAGE_EXT = {"png": "png", "jpeg": "jpg", "gif": "gif", "webp": "webp"}

# Input file MIME/extension: images are verified by real file header (the vision path needs
# it); other types have no universal magic, so record the declared type/extension and store as-is.
_MIME_ALIASES = {
    "image/jpg": "image/jpeg",
    "text/plain": "text/plain",
    "application/x-zip-compressed": "application/zip",
}
_EXT_MIME = {
    "pdf": "application/pdf", "csv": "text/csv", "tsv": "text/tab-separated-values",
    "json": "application/json", "jsonl": "application/x-ndjson", "md": "text/markdown",
    "txt": "text/plain", "log": "text/plain", "html": "text/html", "htm": "text/html",
    "xml": "application/xml", "yaml": "application/yaml", "yml": "application/yaml",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "doc": "application/msword", "xls": "application/vnd.ms-excel",
    "ppt": "application/vnd.ms-powerpoint", "zip": "application/zip",
    "gz": "application/gzip", "tar": "application/x-tar", "7z": "application/x-7z-compressed",
    "mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4",
    "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm", "mkv": "video/x-matroska",
    "srt": "application/x-subrip", "vtt": "text/vtt",
}
# Types the executor reads as text: usable as-is once on disk, no model "vision" needed.
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_EXTRAS = {
    "application/json", "application/x-ndjson", "application/xml",
    "application/yaml", "application/x-yaml", "application/javascript",
    "application/x-subrip", "text/vtt",
}


def _canonical_mime(raw: str) -> str:
    mime = str(raw or "").strip().lower().split(";", 1)[0]
    return _MIME_ALIASES.get(mime, mime)


# ------------------------------------- remote file fetch (ChatGPT attachment passthrough)
#
# What ChatGPT passes through openai/fileParams is not bytes but {download_url, file_id,
# mime_type, file_name}. download_url is only valid during this tool call, so the bytes must be
# pulled to disk synchronously inside it — store the URL and fetch later and it is always expired.
#
# The only spot where an external URL makes this box reach out, so SSRF rules apply, no shortcuts:
FILE_FETCH_TIMEOUT = float(os.environ.get("PAD_FILE_FETCH_TIMEOUT", "120"))
FILE_FETCH_CONNECT_TIMEOUT = float(os.environ.get("PAD_FILE_FETCH_CONNECT_TIMEOUT", "10"))
FILE_FETCH_MAX_REDIRECTS = int(os.environ.get("PAD_FILE_FETCH_MAX_REDIRECTS", "3"))
# No domain restriction: ChatGPT attachments land on oaiusercontent.com, openai.com, or its own
# Azure Blob (the 2026-10-26 MP3 came from oaisdmntprwestus3.blob.core.windows.net). A hardcoded
# allowlist only blocks our own traffic, and every CDN move means editing code. So we constrain
# "protocol + not an internal target" only.
#
# The real boundary is not "which domains to trust" but "never hit the internal network": SSRF
# harm is using the server to reach 127.0.0.1 / 10.x / 169.254.169.254, whatever the public domain.
# So keep: https only, reject private/loopback/reserved after DNS resolution, re-validate each
# redirect hop, plus size and time caps. Need it tighter: pass PAD_FILE_URL_HOSTS (empty = any).
_DEFAULT_FILE_URL_HOSTS = ""


def _file_url_hosts() -> tuple:
    """Optional allowlist. Empty by default = no domain restriction (only https + non-internal).

    To tighten, set PAD_FILE_URL_HOSTS (comma separated); `*.` prefixes match by suffix, anything
    else must match the host exactly. Read at runtime so tests can override it.
    """
    raw = os.environ.get("PAD_FILE_URL_HOSTS", _DEFAULT_FILE_URL_HOSTS)
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def _host_allowed(host: str, hosts: tuple) -> bool:
    if not hosts:
        return True                      # no domain restriction by default
    for pattern in hosts:
        if pattern.startswith("*."):
            if host == pattern[2:] or host.endswith("." + pattern[2:]):
                return True
        elif host == pattern:
            return True
        elif pattern.startswith(".") and host.endswith(pattern):
            return True
    return False


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


class FileFetchError(ValueError):
    """Remote file fetch failed (network/validation/over limit). Carries a human-readable reason."""


def _check_url_allowed(url: str) -> str:
    """Check whether a download URL is fetchable: https + domain allowlist + non-internal IP.

    Every redirect re-runs this gate — validating only the first URL lets one 302 to
    http://127.0.0.1:8791/ store the internal service's response as an attachment.
    """
    # Two unit-test-only switches: the stub server runs on 127.0.0.1 over plain http. Unset in
    # production, which means https + internal rejection, i.e. full SSRF protection.
    allow_http = _flag("PAD_FILE_ALLOW_HTTP")
    allow_loopback = _flag("PAD_FILE_ALLOW_LOOPBACK")
    hosts = _file_url_hosts()

    parts = urllib.parse.urlsplit(url)
    scheme = (parts.scheme or "").lower()
    if scheme != "https" and not (allow_http and scheme == "http"):
        raise FileFetchError(f"只允许 https 下载地址，收到 {scheme or '(空)'}")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise FileFetchError("下载地址缺少主机名")
    if not _host_allowed(host, hosts):
        raise FileFetchError(
            f"下载地址主机 {host} 不在 PAD_FILE_URL_HOSTS 白名单内（该开关默认空=不限制）。")
    # Check every resolved address: an allowlist cannot stop DNS pointing at the internal net.
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FileFetchError(f"下载地址无法解析: {host}") from exc
    if not infos:
        raise FileFetchError(f"下载地址无法解析: {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            if allow_loopback and (ip.is_loopback or ip.is_private):
                continue        # unit tests only; this switch does not exist in production
            raise FileFetchError(f"下载地址解析到内网/保留地址，拒绝拉取: {ip}")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Forbid automatic redirect following.

    urllib follows 302/307 by itself, so "re-validate every hop" never sees the intermediate URL
    — a redirect to the internal network or file:// is silently followed. Here it becomes an
    ordinary 3xx error the caller walks by hand, validating each hop.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_remote_file(url: str, max_bytes: int) -> tuple[bytes, str]:
    """Synchronously fetch remote file bytes and return (data, content_type).

    Count while reading, abort the moment the cap is hit — don't download then judge, or a tens of
    GB response has already eaten the memory and bandwidth. Redirects are walked by hand, per hop.
    """
    current = _check_url_allowed(url)
    for hop in range(FILE_FETCH_MAX_REDIRECTS + 1):
        req = urllib.request.Request(current, headers={
            "User-Agent": "PAD/1.0 (+file-input)",
            "Accept": "*/*",
        })
        try:
            # Critical: disable auto-follow. urllib would follow 302 itself and the hand-written
            # "re-validate every hop" below would never fire — one 302 bypasses the internal block.
            opener = urllib.request.build_opener(_NoRedirect)
            resp = opener.open(req, timeout=FILE_FETCH_CONNECT_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                if hop >= FILE_FETCH_MAX_REDIRECTS:
                    raise FileFetchError("下载地址重定向次数过多") from exc
                # Re-validate every hop: else a 302 stores an internal response as an attachment.
                current = _check_url_allowed(
                    urllib.parse.urljoin(current, exc.headers["Location"]))
                continue
            raise FileFetchError(f"下载失败: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise FileFetchError(f"下载失败: {type(exc).__name__}") from exc

        with resp:
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise FileFetchError(
                    f"远端文件声明 {int(declared)} 字节，超过 {max_bytes} 字节上限")
            ctype = _canonical_mime(resp.headers.get("Content-Type", ""))
            buf = bytearray()
            deadline = time.monotonic() + FILE_FETCH_TIMEOUT
            while True:
                if time.monotonic() > deadline:
                    raise FileFetchError(f"下载超时（{FILE_FETCH_TIMEOUT:.0f}s）")
                chunk = resp.read(1 << 16)
                if not chunk:
                    break
                buf.extend(chunk)
                if len(buf) > max_bytes:
                    raise FileFetchError(f"远端文件超过 {max_bytes} 字节上限，已中止")
            if not buf:
                raise FileFetchError("远端文件内容为空")
            return bytes(buf), ctype
    raise FileFetchError("下载地址重定向次数过多")


def _guess_mime(name: str) -> str:
    ext = str(name).rsplit(".", 1)[-1].lower() if "." in str(name) else ""
    if ext in _EXT_MIME:
        return _EXT_MIME[ext]
    if ext in _RASTER_EXTS:
        return _IMAGE_MIME["jpeg" if ext == "jpg" else ext]
    return _canonical_mime(mimetypes.guess_type(str(name))[0] or "")


def _is_text_mime(mime: str) -> bool:
    return mime.startswith(_TEXT_MIME_PREFIXES) or mime in _TEXT_MIME_EXTRAS


def _normalize_file_entry(item: dict, index: int, names: set, total: int) -> dict:
    """Normalize a file item into an on-disk entry (MIME/extension detection and size checks).

    Source-agnostic: base64-decoded bytes and remote-fetched bytes both land here, so the two
    entry points enforce identical rules (notably "claiming an image means it really is one").
    """
    data = bytes(item["data"])
    if not data:
        raise ValueError(f"第 {index} 个输入文件内容为空")
    if len(data) > db.INPUT_FILE_MAX_BYTES:
        raise ValueError(f"第 {index} 个输入文件超过 {db.INPUT_FILE_MAX_BYTES} 字节上限")
    if total + len(data) > db.INPUT_FILE_MAX_TOTAL_BYTES:
        raise ValueError(f"输入文件合计不能超过 {db.INPUT_FILE_MAX_TOTAL_BYTES} 字节")

    raw_name = str(item.get("name") or f"input-{index}")
    name_ext = raw_name.replace("\\", "/").split("/")[-1].rsplit(".", 1)
    name_ext = name_ext[-1].lower() if len(name_ext) == 2 else ""
    declared = _canonical_mime(item.get("mime_type"))
    # Declared octet-stream / missing → let the file header decide (older callers often do this).
    explicit = "" if declared in ("", "application/octet-stream") else declared
    fmt = _sniff_image(data)
    if fmt:
        actual_mime = _IMAGE_MIME[fmt]
        if explicit and explicit != actual_mime:
            raise ValueError(f"第 {index} 个文件声明为 {explicit}，实际格式为 {actual_mime}")
        declared = actual_mime
        ext = _IMAGE_EXT[fmt]
    elif explicit.startswith("image/") or (not explicit and name_ext in _RASTER_EXTS):
        # Claims an image but the content is not (or the extension does) — this cannot pass.
        raise ValueError(f"第 {index} 个文件声称是图片，但文件内容不是支持的 PNG/JPEG/GIF/WebP")
    else:
        declared = explicit or _guess_mime(raw_name) or "application/octet-stream"
        ext = name_ext
        if not ext or not ext.isascii() or not ext.isalnum() or len(ext) > 8:
            guessed = str(mimetypes.guess_extension(declared) or "").lstrip(".")
            ext = guessed if guessed.isascii() and guessed.isalnum() and len(guessed) <= 8 else "bin"

    raw_stem = raw_name.replace("\\", "/").split("/")[-1].rsplit(".", 1)[0]
    stem = _safe_stem(raw_stem) or f"input-{index}"
    base = f"{stem}.{ext}"
    key = base.casefold()
    suffix = 2
    while key in names:
        base = f"{stem}-{suffix}.{ext}"
        key = base.casefold()
        suffix += 1
    names.add(key)
    entry = {"name": base, "mime_type": declared, "data": data}
    if _is_text_mime(declared):
        # Text types also carry a short preview, so the upper layer needn't re-download to peek.
        entry["preview"] = data[:2000].decode("utf-8", errors="replace")
    return entry


def _from_base64(item: dict, index: int) -> dict:
    """Old path: the caller stuffs base64 bytes into an argument."""
    encoded = (item.get("data_base64") or item.get("data_url")
               or item.get("content_base64"))
    if not isinstance(encoded, str) or not encoded.strip():
        # The most common misuse: passing a local path as the "filename". Explain why it fails.
        hint = ""
        for key in ("path", "name", "file", "filename"):
            val = item.get(key)
            if isinstance(val, str) and val.strip().startswith(("/", "~", "./", "../")) or (
                    isinstance(val, str) and len(val) > 1 and val[1:3] == ":\\"):
                hint = (f"（看清楚：{key}={val!r} 是个本地路径。PAD 跑在服务器上，读不到你"
                        "那边的磁盘 —— 要么让 ChatGPT 用 files 参数传附件引用，"
                        "要么把字节 base64 放进 data_base64。）")
                break
        raise ValueError(
            f"第 {index} 个输入文件缺少 data_base64 或 data_url{hint} "
            "若这是 ChatGPT 上传的附件，用 files 参数（不是 input_files）。")
    declared = _canonical_mime(item.get("mime_type"))
    encoded = encoded.strip()
    if encoded.startswith("data:"):
        header, sep, payload = encoded.partition(",")
        if not sep or ";base64" not in header.lower():
            raise ValueError(f"第 {index} 个输入文件的 data_url 格式无效")
        data_mime = _canonical_mime(header[5:].split(";", 1)[0])
        if declared and data_mime and declared != data_mime:
            raise ValueError(f"第 {index} 个输入文件的 mime_type 与 data_url 不一致")
        declared = declared or data_mime
        encoded = payload
    try:
        data = base64.b64decode("".join(encoded.split()), validate=True)
    except (ValueError, binascii.Error) as exc:
        preview = encoded[:60].replace("\n", " ")
        raise ValueError(
            f"第 {index} 个输入文件不是有效的 base64（开头是 {preview!r}）。"
            "如果这是文件路径或文件 ID，那不是 base64 —— "
            "ChatGPT 附件请走 files 参数。") from exc
    return {"name": item.get("name"), "mime_type": declared, "data": data}


def _from_file_ref(item: dict, index: int) -> dict:
    """New path: ChatGPT's openai/fileParams reference; we fetch the bytes from download_url.

    download_url is only valid during this call, so it must be fetched synchronously; never
    store the URL in the task and let the node pull it later (by then it is always expired).
    """
    url = item.get("download_url") or item.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"第 {index} 个输入文件缺少 download_url")
    expected = db.INPUT_FILE_MAX_BYTES
    data, fetched_mime = _fetch_remote_file(url.strip(), expected)
    # Prefer the original name ChatGPT gave us; fall back to file_id, and only then input-N.
    name = item.get("file_name") or item.get("name")
    if not name and item.get("file_id"):
        name = str(item["file_id"])
    # Declared MIME leads but measurement backstops: ChatGPT's mime_type is sometimes empty and
    # HTTP headers often say octet-stream; neither should override the real file header.
    declared = _canonical_mime(item.get("mime_type")) or fetched_mime
    return {"name": name, "mime_type": declared, "data": data}


def _prepare_input_files(files) -> list[dict]:
    """Validate files from MCP/REST, decode/fetch them uniformly, hand them to task input storage.

    Each element may be one of three shapes:
      1. {name, mime_type, data_base64}        — the old path, bytes travel with the argument
      2. {download_url, file_id, mime_type, file_name} — a ChatGPT attachment reference (fileParams)
      3. {path}                              — rejected outright: PAD cannot read the caller's disk

    Images (PNG/JPEG/GIF/WebP) must be confirmed by the real file header, and a declaration that
    disagrees is rejected — the upper layer uses "this is an image" as truth, so a fake header lies.
    """
    if files is None:
        return []
    if not isinstance(files, list):
        raise ValueError("input_files 必须是文件对象数组")
    if len(files) > db.INPUT_FILE_MAX_COUNT:
        raise ValueError(f"每个任务最多接收 {db.INPUT_FILE_MAX_COUNT} 个输入文件")

    prepared, total, names = [], 0, set()
    for i, item in enumerate(files, 1):
        # Two sources: MCP's files parameter yields pydantic models, REST / the old path yields
        # plain dicts. Flatten both to dicts here; validation below only knows dicts.
        if isinstance(item, BaseModel):
            item = item.model_dump(exclude_none=True)
        if not isinstance(item, dict):
            raise ValueError(f"第 {i} 个输入文件必须是对象")
        # Local paths like /mnt/data are unreadable from PAD; say so early instead of letting it
        # become "looks passed, actually empty" or get base64-decoded.
        if item.get("path") and not (item.get("data_base64") or item.get("download_url")):
            raise ValueError(
                f"第 {i} 个输入文件只给了 path（{item.get('path')!r}）。PAD 运行在服务器上，"
                "读不到调用方的本地路径；请用 download_url 文件引用或 data_base64 传字节。")
        try:
            if item.get("download_url") or item.get("url"):
                src = _from_file_ref(item, i)
            else:
                src = _from_base64(item, i)
            entry = _normalize_file_entry(src, i, names, total)
        except FileFetchError as exc:
            raise ValueError(f"第 {i} 个输入文件拉取失败：{exc}") from exc
        total += len(entry["data"])
        prepared.append(entry)
    return prepared


def _attachment_disposition(name: str) -> str:
    """Content-Disposition per RFC 5987: a Chinese name goes in filename*, plus an ASCII fallback.

    HTTP headers take latin-1 only; a Chinese name in filename="..." makes the download 500.
    """
    ascii_name = name.encode("ascii", "ignore").decode().strip() or "input"
    return (f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{urllib.parse.quote(name, safe='')}")


def _safe_stem(raw: str, limit: int = 60) -> str:
    """Keep the filename readable (Chinese too); strip only path separators and control chars.

    The old implementation was an ASCII-only allowlist, so a Chinese filename became "input-1.pdf" —
    the executor saw a pile of input-N in inputs/ and lost the context. Now only necessary cleaning.
    """
    stem = str(raw or "").replace("\\", "/").split("/")[-1]
    stem = "".join(c for c in stem if c.isprintable() and c not in '/\\:*?"<>|')
    stem = stem.strip(" .")
    return stem[:limit]


# Compatible with the old call name (the method name from the image era).
_prepare_input_images = _prepare_input_files


def _artifact_chunk(task_id, name, offset: int, limit: int) -> dict:
    """Text artifact → paginated dict; image artifact → use get_artifact (no binary decode)."""
    blob = db.read_artifact_bytes(task_id, name)
    if "error" not in blob and _image_format(blob["meta"]["name"], blob["data"]):
        return {"name": blob["meta"]["name"], "bytes": blob["meta"]["bytes"],
                "sha256": blob["meta"]["sha256"], "mime": "image",
                "note": "图片产物不分页；请调用 get_artifact 取回原生图片内容。"}
    return db.read_artifact(task_id, name, offset, limit)


# ----------------------------------------------------------------- MCP tools

@mcp.tool()
def get_digest(recent_limit: int = 10) -> dict:
    """Global picture. nodes are the machines you can direct:
       - description says what this machine is for
       - capabilities are the jobs it takes (submit with this name)
       - capabilities_detail explains each capability in Chinese
       - nodes with input_files_supported true take files with a task (old: input_images_supported)
       - recent_tasks are its latest tasks by updated_at desc (task_id/status/updated_at each);
         recent_limit controls how many per machine (default 10). Details: get_task(task_id).
       Also reports how many tasks are running and how many finished today."""
    return db.digest(server_bundle=SERVER_BUNDLE, recent_limit=recent_limit)


@mcp.tool(meta={"openai/fileParams": ["files"]})
def submit_task(capability: str, goal: str, constraints: list | None = None,
                acceptance: list | None = None, deadline: str = "",
                priority: str = "normal",
                files: list[OpenAIFileParam] = Field(default_factory=list),
                input_files: list[dict] | None = None,
                input_images: list[dict] | None = None) -> dict:
    """Submit a task. Pick capability from the get_digest node list, never a machine name.
    goal says what you want, not the steps; acceptance holds machine-checkable criteria.

    There are two ways to ship files with a task, use whichever fits:

    1) files — attachments the user uploaded in ChatGPT (MP3/MP4/PDF/image/spreadsheet…).
       Put them in the files array; the server fetches the raw bytes from download_url itself,
       so the model need not and should not read file contents into context.
    2) input_files — when the caller already has the bytes, each {name, mime_type, data_base64}
       (a full data:...;base64,... data URL is also accepted).

    Both paths share the same checks and on-disk handling: an image's header must match what it
    claims, other types are kept as-is. The node gets files under inputs/ and the execution prompt
    lists their paths and sha256. At most 8 files, ≤10 MiB each, ≤15 MiB total (server tunable).

    input_images is a historical parameter name, equivalent to input_files."""
    merged = list(files or []) + list(input_files if input_files is not None
                                     else (input_images or []))
    try:
        prepared = _prepare_input_files(merged)
    except ValueError as exc:
        # Critical: without ToolError the SDK treats this as a crash and the model only sees
        # 「Error executing tool submit_task」 — the real reason (blocked domain, wrong field)
        # stays in the server log and the caller only gets INVALID_ARGUMENT.
        raise ToolError(f"文件接收失败：{exc}") from exc
    return db.submit_task(capability, goal, constraints, acceptance,
                          deadline or None, priority, created_by="chatgpt",
                          input_files=prepared)


@mcp.tool(meta={"openai/fileParams": ["files"]})
def probe_file_upload(files: list[OpenAIFileParam]) -> dict:
    """File intake self-check: really receive the ChatGPT attachment, report bytes and SHA-256.

    Only verifies "can an attachment reach the server". Real work: use submit_task's files.
    Call it by putting the uploaded attachment in files; no need to read the contents.
    Returns name / mime_type / bytes / sha256 / on-disk path per file, to compare with the
    local source (identical SHA-256 means byte-identical)."""
    if not files:
        return {"ok": False, "error": "没有收到文件引用，files 为空"}
    try:
        prepared = _prepare_input_files(list(files))
    except ValueError as exc:
        raise ToolError(f"文件接收失败：{exc}") from exc
    root = db.INPUT_FILE_ROOT / "_probe" / db.new_id("probe")
    root.mkdir(parents=True, exist_ok=True)
    out = []
    for entry in prepared:
        path = root / entry["name"]
        path.write_bytes(entry["data"])
        out.append({
            "name": entry["name"],
            "mime_type": entry["mime_type"],
            "bytes": len(entry["data"]),
            "sha256": hashlib.sha256(entry["data"]).hexdigest(),
            "saved_path": str(path),
        })
    return {"ok": True, "received": len(out), "files": out}


@mcp.tool()
def close_task(task_id: str, reason: str = "") -> dict:
    """Cancel a task; the node terminates the corresponding local execution process group."""
    return db.close_task(task_id, reason, by="chatgpt")


@mcp.tool()
def get_task(task_id: str, artifact: str = "", offset: int = 0, limit: int = 8000) -> dict:
    """Inspect a task: status, constraints, acceptance, plus progress and the final report.

    Read reports[].payload:
      - payload.report         acceptance report body (what the job became, how each part went)
      - payload.summary        one-line conclusion
      - payload._acceptance_check  per-item acceptance coverage; missing lists unanswered ones
      - payload.artifacts      artifact list (local paths/sizes/row counts/schema if not uploaded)

    With artifact (a name from artifacts[].name) only one chunk comes back (8000 bytes default);
    page with offset. For image artifacts use get_artifact.
    input_files[] (old alias input_images[]) is the input file metadata, without bytes —
    use get_input_file(task_id, name) for content."""
    view = db.task_view(task_id) or {"error": "not_found", "task_id": task_id}
    if artifact and "error" not in view:
        view["artifact_chunk"] = _artifact_chunk(task_id, artifact, offset, limit)
    return view


@mcp.tool()
def get_artifact(task_id: str, name: str = "", offset: int = 0, limit: int = 8000) -> dict | Image:
    """Open an artifact's content ("open the box"). get_task says what exists, this opens it.

    Take name from artifacts[].name in get_task; leaving it empty lists the task's artifacts and
    returns no content (a wrong name also lists the available names).

    Image artifacts (image/png, image/jpeg, image/gif, image/webp) return native image content
    directly (MCP ImageContent), which the upper layer can read visually; no pagination.

    Text artifacts paginate by bytes: content is this chunk, a non-empty next_offset continues from
    there, starting at 0. 8000 bytes per call by default, up to 200000 — never the whole blob."""
    view = db.task_view(task_id)
    if view is None:
        return {"error": "not_found", "task_id": task_id}
    if not name:
        return {"task_id": task_id, "artifacts": view["artifacts"],
                "hint": "把 artifacts[].name 传给 name 参数取正文"}
    blob = db.read_artifact_bytes(task_id, name)
    if "error" in blob:
        return blob
    fmt = _image_format(blob["meta"]["name"], blob["data"])
    if fmt:
        # Images go back as native content blocks; a binary slice decodes only into garbage.
        return Image(data=blob["data"], format=fmt)
    return db.read_artifact(task_id, name, offset, limit)


@mcp.tool()
def get_input_file(task_id: str, name: str = "", offset: int = 0, limit: int = 8000):
    """Read back an input file shipped with a task (open what get_task's input_files[] lists).

    An empty name only lists the input files (name/mime_type/bytes/sha256).
    Images return native image content (MCP ImageContent), readable visually, no pagination.
    Text types (text/*, json, csv, md, srt…) paginate by bytes, returning content + next_offset.
    Other binaries (pdf/zip/mp4…) don't paginate: metadata plus a hint to let the node handle it,
    never stuff binary into context."""
    view = db.task_view(task_id)
    if view is None:
        return {"error": "not_found", "task_id": task_id}
    if not name:
        return {"task_id": task_id, "input_files": view["input_files"],
                "hint": "把 input_files[].name 传给 name 参数取内容"}
    blob = db.read_task_input_file(task_id, name)
    if "error" in blob:
        return blob
    meta, data = blob["meta"], blob["data"]
    fmt = _image_format(meta["name"], data)
    if fmt:
        return Image(data=data, format=fmt)
    mime = _canonical_mime(meta["mime_type"])
    if not _is_text_mime(mime):
        return {"name": meta["name"], "mime_type": mime, "bytes": meta["bytes"],
                "sha256": meta["sha256"],
                "note": "二进制输入文件不分页；内容已随任务落在节点的 inputs/，"
                        "要看内容请让节点读取后回传文本或提取结果。"}
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 200000))
    chunk = data[offset:offset + limit]
    return {"name": meta["name"], "mime_type": mime, "bytes": meta["bytes"],
            "sha256": meta["sha256"], "offset": offset, "returned": len(chunk),
            "next_offset": offset + len(chunk) if offset + len(chunk) < len(data) else None,
            "content": chunk.decode("utf-8", errors="replace")}


@mcp.tool()
def get_artifact_delivery(task_id: str, name: str, ttl_sec: int | None = None) -> dict:
    """Turn a registered PAD artifact into a short-lived signed HTTPS delivery link (for a human).

    Returns the pad.artifact/v2 contract, where:
      - delivery.open_url      open/play inline in the browser
      - delivery.download_url  force save-as
      - delivery.sha256 / size / mime_type  for verification
    Zero registration: no *.artifact.json to create, no worker task. task_id / name come from
    artifacts[].name in get_task. PAD calls the Artifact Bridge internally to "confirm registered
    → lazily materialize → sign"; the Bridge key never passes through or comes back to the caller.

    ttl_sec is optional, the signature lifetime in seconds (Bridge default 3600)."""
    if not AB_DELIVERY_KEY:
        return {"error": "delivery_unavailable",
                "detail": "PAD server has no AB_DELIVERY_KEY configured."}
    params = {"task_id": task_id, "name": name}
    if ttl_sec:
        params["ttl"] = int(ttl_sec)
    sep = "&" if "?" in AB_DELIVERY_URL else "?"
    url = f"{AB_DELIVERY_URL}{sep}{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"X-Delivery-Key": AB_DELIVERY_KEY})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            payload = {}
        return {"error": payload.get("error", f"bridge_http_{exc.code}"),
                "status": exc.code, "task_id": task_id, "name": name}
    except OSError as exc:
        return {"error": "bridge_unreachable", "detail": type(exc).__name__,
                "task_id": task_id, "name": name}


@mcp.tool()
def get_artifact_observe(task_id: str, name: str, frames: int | None = None,
                         ttl_sec: int | None = None
                         ) -> list[TextContent | ImageContent | ResourceLink | EmbeddedResource]:
    """Fetch a registered PAD artifact as native content the model can observe directly (on demand).

    Image artifacts (image/*) return native MCP image content, readable visually.
    Video artifacts (video/*) return frames decoded from the real bytes at points in time as native
    image content (each preceded by a t=…s text), plus a resource_link to the whole file; frames
    sets how many are extracted (default decided server-side).
    Other files return a resource_link (small ones also embed content).

    task_id / name come from artifacts[].name in get_task. Zero registration: no *.artifact.json
    to create, no worker task. PAD forwards the request to the observe plane, which does "confirm
    registered → lazily materialize → decode frames"; the internal key never passes through or back.

    ttl_sec is optional, the lifetime of the returned full-file link in seconds."""
    if not AB_DELIVERY_KEY:
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(
            {"error": "observe_unavailable",
             "detail": "PAD server has no observe key configured."}))], is_error=True)
    params = {"task_id": task_id, "name": name}
    if frames is not None:
        params["frames"] = int(frames)
    if ttl_sec:
        params["ttl"] = int(ttl_sec)
    sep = "&" if "?" in AB_OBSERVE_URL else "?"
    url = f"{AB_OBSERVE_URL}{sep}{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"X-Delivery-Key": AB_DELIVERY_KEY})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read().decode("utf-8")).get("error")
        except (ValueError, OSError):
            err = None
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(
            {"error": err or f"observe_http_{exc.code}", "status": exc.code,
             "task_id": task_id, "name": name}))], is_error=True)
    except OSError as exc:
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(
            {"error": "observe_unreachable", "detail": type(exc).__name__,
             "task_id": task_id, "name": name}))], is_error=True)
    blocks = payload.get("content") if isinstance(payload, dict) else None
    if not blocks:
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(
            {"error": "observe_empty", "task_id": task_id, "name": name}))],
            is_error=True)
    return _to_content_blocks(blocks)


# ----------------------------------------------------------------- REST app

@asynccontextmanager
async def lifespan(app):
    db.init()
    print("artifact sweep:", db.sweep_artifacts())
    print("input file sweep:", db.sweep_input_files())
    async with mcp.session_manager.run():
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def guard(request: Request, call_next):
    if not request.url.path.startswith(f"/pad/{PAD_KEY}/"):
        return PlainTextResponse("not found", status_code=404)
    return await call_next(request)


@app.exception_handler(ValueError)
async def bad_request(request, exc):
    return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


# ---- LD side ----------------------------------------------------------------

@app.post("/pad/{key}/api/register")
async def api_register(payload: dict):
    meta = payload.get("meta") or {}
    meta["bundle_hash"] = payload.get("bundle_hash", "")
    out = db.register_node(payload["node_id"], payload.get("role", ""),
                           payload.get("capabilities", []), meta,
                           payload.get("description", ""))
    out["server_bundle_hash"] = SERVER_BUNDLE       # the node compares it against its own
    return out


@app.post("/pad/{key}/api/heartbeat")
async def api_heartbeat(payload: dict):
    return db.heartbeat(payload["node_id"], payload.get("summary", ""))


@app.post("/pad/{key}/api/claim")
async def api_claim(payload: dict):
    task = db.claim_task(payload["node_id"], payload.get("capabilities", []),
                         server_bundle=SERVER_BUNDLE)
    return {"task": task}


@app.post("/pad/{key}/api/report")
async def api_report(payload: dict):
    return db.report(payload["task_id"], payload["node_id"], payload["level"],
                     payload.get("summary", ""), payload.get("payload"),
                     client_id=payload.get("client_id"))


@app.get("/pad/{key}/api/node/{node_id}/tasks")
async def api_node_tasks(node_id: str, status: str = "claimed,running"):
    return {"tasks": db.node_tasks(node_id, [s for s in status.split(",") if s])}


# ---- admin side (padctl / the brain's degraded channel) -------------------------------

@app.get("/pad/{key}/api/digest")
async def api_digest(recent_limit: int = 10):
    return db.digest(SERVER_BUNDLE, recent_limit)


@app.post("/pad/{key}/api/node/{node_id}/forget")
async def api_forget_node(node_id: str):
    return db.forget_node(node_id)


@app.get("/pad/{key}/api/nodes")
async def api_nodes():
    return db.list_nodes()


@app.post("/pad/{key}/api/task")
async def api_submit(payload: dict):
    files = _prepare_input_files(payload.get("input_files")
                                 if payload.get("input_files") is not None
                                 else payload.get("input_images"))
    return db.submit_task(payload["capability"], payload["goal"],
                          payload.get("constraints"), payload.get("acceptance"),
                          payload.get("deadline"), payload.get("priority", "normal"),
                          created_by=payload.get("created_by", "human"),
                          input_files=files)


@app.get("/pad/{key}/api/task/{task_id}")
async def api_task(task_id: str, artifact: str = "", offset: int = 0, limit: int = 8000):
    view = db.task_view(task_id) or {"error": "not_found", "task_id": task_id}
    if artifact and "error" not in view:
        view["artifact_chunk"] = db.read_artifact(task_id, artifact, offset, limit)
    return view


@app.get("/pad/{key}/api/task/{task_id}/input-image")
@app.get("/pad/{key}/api/task/{task_id}/input-file")
async def api_task_input_file(task_id: str, name: str):
    """Lets the node that claimed this task download the raw input files (image/PDF/CSV/zip…)."""
    out = db.read_task_input_file(task_id, name)
    if "error" in out:
        return JSONResponse(out, status_code=404 if out["error"] == "not_found" else 500)
    meta = out["meta"]
    return Response(out["data"], media_type=meta["mime_type"], headers={
        # Headers take latin-1 only; a Chinese name needs RFC 5987 filename*, or the download 500s.
        "Content-Disposition": _attachment_disposition(meta["name"]),
        "X-PAD-SHA256": meta["sha256"],
        "Cache-Control": "no-store",
    })


@app.get("/pad/{key}/api/task/{task_id}/status")
async def api_task_status(task_id: str):
    """Lightweight status: the executor polls it while running, and kills the group on cancelled."""
    st = db.task_status(task_id)
    return st or {"error": "not_found", "task_id": task_id}


@app.get("/pad/{key}/api/task/{task_id}/artifact")
async def api_task_artifact(task_id: str, name: str = "", offset: int = 0, limit: int = 8000):
    """Fetch artifact content. Empty name lists artifacts; a name returns one offset/limit chunk."""
    view = db.task_view(task_id)
    if view is None:
        return {"error": "not_found", "task_id": task_id}
    if not name:
        return {"task_id": task_id, "artifacts": view["artifacts"]}
    return db.read_artifact(task_id, name, offset, limit)


@app.post("/pad/{key}/api/artifact")
async def api_artifact(request: Request, task_id: str, name: str, node_id: str = "",
                       rows: str = "", schema: str = "", local_path: str = ""):
    """The node uploads a copy of an important artifact (raw bytes). Over the cap: rejected."""
    data = await request.body()
    return db.save_artifact(task_id, node_id, name, data, rows=rows, schema=schema,
                            local_path=local_path)



@app.post("/pad/{key}/api/task/{task_id}/close")
async def api_close_task(task_id: str, payload: dict):
    return db.close_task(task_id, payload.get("reason", ""), payload.get("by", "human"))


@app.get("/pad/{key}/join.md")
async def api_join_md():
    """The onboarding protocol for the agent on a new machine."""
    doc = (HERE / "JOIN.md")
    return PlainTextResponse(doc.read_text() if doc.exists() else "JOIN.md missing")


@app.get("/pad/{key}/agent.bundle.tgz")
async def api_agent_bundle():
    """Client skeleton: unpack it on a new machine and it runs."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for rel in bundle.BUNDLE_FILES:
            f = HERE / rel
            if f.exists():
                tar.add(f, arcname=rel)
    return Response(buf.getvalue(), media_type="application/gzip",
                    headers={"Content-Disposition": 'attachment; filename="pad-agent.tgz"'})


app.mount("/pad", mcp.streamable_http_app(
    streamable_http_path=f"/{PAD_KEY}/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=ALLOWED_HOSTS,
        allowed_origins=[f"https://{h}" for h in ALLOWED_HOSTS
                         if not h.startswith(("127.", "localhost")) and not h.endswith(":*")],
    ),
))
