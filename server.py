"""PAD server: one process, SQLite state, two doors.

  /pad/<KEY>/mcp            MCP (给脑 / ChatGPT)
  /pad/<KEY>/api/...        REST (给各台机器的 LD agent)

The brain only ever sees 5 coarse tools. It has no shell, no machine id, no repo.
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
# MCP 的 DNS-rebinding 白名单是**逐字匹配 Host 头**，而 Host 头带端口。
# 只写 `127.0.0.1` 时，`127.0.0.1:8790` 会被判 Invalid Host → 421（现象很像"服务挂了"）。
# 所以对没写端口的条目补一条 `:*` 通配，让它在本机任意端口都成立。
ALLOWED_HOSTS += [f"{h}:*" for h in list(ALLOWED_HOSTS) if ":" not in h]

# artifacts 的 human-delivery 平面不在 PAD 内：PAD 只做最薄代理，把
# get_artifact_delivery(task_id, name) 转给同机的 Artifact Bridge（内部调用，
# 密钥不经过 MCP、不返回给 ChatGPT）。Bridge 负责校验已登记 / 惰性物化 / 签名。
# 同理 get_artifact_observe 只是把请求转给 Bridge 的 observe 平面，解码/抽帧/
# 安全全在 Bridge，PAD 一行不复制。
AB_DELIVERY_URL = os.environ.get("AB_DELIVERY_URL", "http://127.0.0.1:8791/delivery")
AB_DELIVERY_KEY = os.environ.get("AB_DELIVERY_KEY", "")


def _derive_observe_url(delivery_url: str) -> str:
    """observe 与 delivery 同机同服务，默认按 delivery 网址推兄弟端点。"""
    base = (delivery_url or "").rstrip("/")
    if base.endswith("/delivery"):
        return base[: -len("/delivery")] + "/observe"
    return f"{base}/observe"


AB_OBSERVE_URL = (os.environ.get("AB_OBSERVE_URL", "").strip()
                  or _derive_observe_url(AB_DELIVERY_URL))

# Bridge 的 observe 返回的是按 MCP 线上格式（camelCase）写的 content 数组；
# 这里把它还原成原生 content block 对象（尤其 video 的 ImageContent），
# 绝不能当 JSON 文本回给模型。
_CONTENT_BLOCK_ADAPTER = TypeAdapter(ContentBlock)


def _to_content_blocks(blocks: list) -> list:
    out: list = []
    for b in blocks:
        try:
            out.append(_CONTENT_BLOCK_ADAPTER.validate_python(b))
        except Exception:
            # 单个块不可解析也不能吞掉整次调用；降级成文本并保留可读信息。
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


# --------------------------------------------------- artifact MIME 分流
# 产物落库只存字节、不存 MIME，所以这里按「文件头优先、扩展名兜底」识别图片。
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpeg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)
_RASTER_EXTS = {"png", "jpg", "jpeg", "gif", "webp"}


class OpenAIFileParam(BaseModel):
    """ChatGPT 的 openai/fileParams 文件引用。

    形状由 OpenAI 规范钉死，不是随便定的：
      - 四个属性必须全部声明（mime_type / file_name 可以不 required）；
      - required 必须**正好**是 ["download_url", "file_id"]；
      - 多声明一个 required 属性、或把可选属性标成 required，Scan Tools 直接拒收。

    所以这里用 extra="forbid" + 显式 Optional，让生成的 JSON Schema 正好落进规范。
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
    """只按真实文件头判图片格式。输入端不能用扩展名兜底（那是给旧产物分流的）。"""
    head = bytes(data[:16])
    for magic, fmt in _IMAGE_MAGIC:
        if head.startswith(magic):
            return fmt
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    return None


def _image_format(name: str, data: bytes) -> str | None:
    """命中返回 Image() 要的 format（png/jpeg/gif/webp），否则 None。"""
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

# 输入文件的 MIME/扩展名：图片要按真实文件头校验（视觉链路依赖它），
# 其余文件没有可用的万能魔数，按声明/扩展名记账后原样落盘交给执行体。
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
# 会被执行体当文本读的类型：落盘后原样可用，无需模型"看图"能力。
_TEXT_MIME_PREFIXES = ("text/",)
_TEXT_MIME_EXTRAS = {
    "application/json", "application/x-ndjson", "application/xml",
    "application/yaml", "application/x-yaml", "application/javascript",
    "application/x-subrip", "text/vtt",
}


def _canonical_mime(raw: str) -> str:
    mime = str(raw or "").strip().lower().split(";", 1)[0]
    return _MIME_ALIASES.get(mime, mime)


# ------------------------------------------- 远端文件拉取（ChatGPT 附件直传）
#
# ChatGPT 通过 openai/fileParams 传过来的不是字节，是 {download_url, file_id,
# mime_type, file_name}。download_url 只在本次工具调用期间有效，所以必须在调用
# 内部同步把字节取回来落盘 —— 存个 URL 等节点稍后再拉，届时 100% 已过期。
#
# 这是唯一"由外部 URL 触发本机出网"的地方，所以按 SSRF 处理，不做"信任上游"的简化：
FILE_FETCH_TIMEOUT = float(os.environ.get("PAD_FILE_FETCH_TIMEOUT", "120"))
FILE_FETCH_CONNECT_TIMEOUT = float(os.environ.get("PAD_FILE_FETCH_CONNECT_TIMEOUT", "10"))
FILE_FETCH_MAX_REDIRECTS = int(os.environ.get("PAD_FILE_FETCH_MAX_REDIRECTS", "3"))
# 域名不做限制：ChatGPT 的附件可能落在 oaiusercontent.com、openai.com，也可能落在
# 它自有的 Azure Blob（实测 2026-10-26 的 MP3 来自
# oaisdmntprwestus3.blob.core.windows.net）。写死白名单的结果就是自己人拦自己人，
# 而且 CDN 换了还要改代码。所以只约束"协议 + 不能是内网目标"。
#
# 真正的安全边界不是"信任哪些域名"，而是"不许打内网"：SSRF 的危害在于借服务端
# 之手访问 127.0.0.1 / 10.x / 169.254.169.254 这类目标，跟外网域名是哪个无关。
# 所以下面保留：只允许 https、DNS 解析后拒绝内网/回环/保留地址、逐跳重定向重校验、
# 体积与时间上限。需要收紧时用 PAD_FILE_URL_HOSTS 显式给白名单（留空=不限制）。
_DEFAULT_FILE_URL_HOSTS = ""


def _file_url_hosts() -> tuple:
    """可选白名单。默认空 = 不限制域名（只受 https + 非内网约束）。

    要收紧就设 PAD_FILE_URL_HOSTS（逗号分隔）；元素以 `*.` 开头按后缀匹配，
    否则要求宿主完全相同。运行时读，便于测试覆盖。
    """
    raw = os.environ.get("PAD_FILE_URL_HOSTS", _DEFAULT_FILE_URL_HOSTS)
    return tuple(s.strip().lower() for s in raw.split(",") if s.strip())


def _host_allowed(host: str, hosts: tuple) -> bool:
    if not hosts:
        return True                      # 默认不限制域名
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
    """拉取远端文件失败（网络/校验/超限）。带上给人看的原因。"""


def _check_url_allowed(url: str) -> str:
    """校验一次下载 URL 是否可拉：https + 域名白名单 + 非内网 IP。

    每个重定向都要重新过这一关 —— 只校验首个 URL 的话，一个 302 指向
    http://127.0.0.1:8791/ 就能把内网服务的响应当附件存下来。
    """
    # 两个"只给单元测试"的开关：桩服务跑在 127.0.0.1 且是明文 http。
    # 生产环境不设它们，等于 https + 拒绝内网，SSRF 防护完整。
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
    # 解析后逐个检查：域名白名单挡不住 DNS 指着内网的情况。
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
                continue        # 单元测试专用；生产环境此开关不存在
            raise FileFetchError(f"下载地址解析到内网/保留地址，拒绝拉取: {ip}")
    return url


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止自动跟随重定向。

    urllib 默认会自动顺着 302/307 走完，这意味着"每一跳重新校验"永远看不到
    中间的 URL —— 一个指向内网或 file:// 的跳转就被静默跟过去了。这里把它变成
    一个普通 3xx 错误，交给上层手动跟并逐跳校验。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _fetch_remote_file(url: str, max_bytes: int) -> tuple[bytes, str]:
    """同步取回远端文件字节，返回 (data, content_type)。

    边读边计数，超过上限立刻中止 —— 不先下完再判断，否则一个几十 GB 的响应
    就已经把内存和带宽吃掉了。重定向手动跟随，每一跳重新做安全检查。
    """
    current = _check_url_allowed(url)
    for hop in range(FILE_FETCH_MAX_REDIRECTS + 1):
        req = urllib.request.Request(current, headers={
            "User-Agent": "PAD/1.0 (+file-input)",
            "Accept": "*/*",
        })
        try:
            # 关键：必须禁掉自动跟跳。urllib 默认会自己顺着 302 走，那样下面手写的
            # "每一跳重新校验" 根本不会触发 —— 一个 302 就能绕过内网拦截。
            opener = urllib.request.build_opener(_NoRedirect)
            resp = opener.open(req, timeout=FILE_FETCH_CONNECT_TIMEOUT)
        except urllib.error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308) and exc.headers.get("Location"):
                if hop >= FILE_FETCH_MAX_REDIRECTS:
                    raise FileFetchError("下载地址重定向次数过多") from exc
                # 每一跳都重新过安全校验：否则一个 302 就能把内网服务当附件存下来。
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
    """把一个文件项规整成落盘条目（含 MIME/扩展名判定与体积校验）。

    来源无关：base64 解码后的字节、远端拉回的字节都走这里，保证两条入口的
    校验规则一模一样（尤其"声称是图片就必须真是图片"这条）。
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
    # 声明成 octet-stream / 不写，就让文件头说了算（老调用方常这么传）。
    explicit = "" if declared in ("", "application/octet-stream") else declared
    fmt = _sniff_image(data)
    if fmt:
        actual_mime = _IMAGE_MIME[fmt]
        if explicit and explicit != actual_mime:
            raise ValueError(f"第 {index} 个文件声明为 {explicit}，实际格式为 {actual_mime}")
        declared = actual_mime
        ext = _IMAGE_EXT[fmt]
    elif explicit.startswith("image/") or (not explicit and name_ext in _RASTER_EXTS):
        # 声称是图片、内容却不是（或按扩展名说是图片、内容不是）——不能放过去。
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
        # 文本类顺带带一小段预览：上层不用为"看一眼写了什么"再走一轮下载。
        entry["preview"] = data[:2000].decode("utf-8", errors="replace")
    return entry


def _from_base64(item: dict, index: int) -> dict:
    """老路径：调用方把字节 base64 塞进参数。"""
    encoded = (item.get("data_base64") or item.get("data_url")
               or item.get("content_base64"))
    if not isinstance(encoded, str) or not encoded.strip():
        # 最常见的误用：把本地路径当成"文件名"塞进来。说清楚它为什么不行。
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
    """新路径：ChatGPT 的 openai/fileParams 文件引用，带 download_url 自己取字节。

    download_url 只在本次调用期间有效，所以这里必须同步取完；绝不能把 URL 存进
    任务里让节点稍后再拉（那时必然过期）。
    """
    url = item.get("download_url") or item.get("url")
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"第 {index} 个输入文件缺少 download_url")
    expected = db.INPUT_FILE_MAX_BYTES
    data, fetched_mime = _fetch_remote_file(url.strip(), expected)
    # 名字优先用 ChatGPT 给的原名；拿不到就用 file_id，最后才 input-N。
    name = item.get("file_name") or item.get("name")
    if not name and item.get("file_id"):
        name = str(item["file_id"])
    # MIME 以声明为准但让实测兜底：ChatGPT 的 mime_type 有时是空的，
    # 而 HTTP 头常给 application/octet-stream，两者都不该盖掉真实文件头。
    declared = _canonical_mime(item.get("mime_type")) or fetched_mime
    return {"name": name, "mime_type": declared, "data": data}


def _prepare_input_files(files) -> list[dict]:
    """验证 MCP/REST 传入的文件，统一解码/拉取后交给任务输入存储。

    每个元素可以是三种形态之一：
      1. {name, mime_type, data_base64}        —— 老路径，字节随参数走
      2. {download_url, file_id, mime_type, file_name} —— ChatGPT 附件引用（fileParams）
      3. {path}                                —— 明确拒绝：PAD 读不到调用方的磁盘

    图片（PNG/JPEG/GIF/WebP）必须由真实文件头确认，声明与实际不符直接拒 ——
    上层看到"这是图片"就会当图片用，头是假的等于骗它。
    """
    if files is None:
        return []
    if not isinstance(files, list):
        raise ValueError("input_files 必须是文件对象数组")
    if len(files) > db.INPUT_FILE_MAX_COUNT:
        raise ValueError(f"每个任务最多接收 {db.INPUT_FILE_MAX_COUNT} 个输入文件")

    prepared, total, names = [], 0, set()
    for i, item in enumerate(files, 1):
        # 两种来源：MCP 的 files 参数是 pydantic 模型，REST/老路径是原始 dict，
        # 这里统一拍平，后面的校验只认 dict。
        if isinstance(item, BaseModel):
            item = item.model_dump(exclude_none=True)
        if not isinstance(item, dict):
            raise ValueError(f"第 {i} 个输入文件必须是对象")
        # /mnt/data 之类的本地路径 PAD 根本读不到，早点说清楚，别让它变成
        # "看起来传了、实际空文件"或者被当成 base64 去 decode。
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
    """按 RFC 5987 构造 Content-Disposition：中文名走 filename*，同时给 ASCII 兜底。

    HTTP 头只接受 latin-1，直接把「季度报告.pdf」拼进 filename="..." 会让节点下载 500。
    """
    ascii_name = name.encode("ascii", "ignore").decode().strip() or "input"
    return (f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{urllib.parse.quote(name, safe='')}")


def _safe_stem(raw: str, limit: int = 60) -> str:
    """保留可读文件名（中文也留），只剔掉路径分隔符、控制字符和会毁 URL/落盘的符号。

    老实现是 ASCII-only 白名单，结果「季度报告.pdf」变成「input-1.pdf」——
    执行体在 inputs/ 里看到一堆 input-N，等于把上下文丢了。现在只做必要清洗。
    """
    stem = str(raw or "").replace("\\", "/").split("/")[-1]
    stem = "".join(c for c in stem if c.isprintable() and c not in '/\\:*?"<>|')
    stem = stem.strip(" .")
    return stem[:limit]


# 兼容老调用名（图片时代的方法名）。
_prepare_input_images = _prepare_input_files


def _artifact_chunk(task_id, name, offset: int, limit: int) -> dict:
    """文本产物→原分页 dict；图片产物→提示改用 get_artifact（别把二进制 decode 成乱码）。"""
    blob = db.read_artifact_bytes(task_id, name)
    if "error" not in blob and _image_format(blob["meta"]["name"], blob["data"]):
        return {"name": blob["meta"]["name"], "bytes": blob["meta"]["bytes"],
                "sha256": blob["meta"]["sha256"], "mime": "image",
                "note": "图片产物不分页；请调用 get_artifact 取回原生图片内容。"}
    return db.read_artifact(task_id, name, offset, limit)


# ----------------------------------------------------------------- MCP tools

@mcp.tool()
def get_digest(recent_limit: int = 10) -> dict:
    """全局态势。nodes 里是你能指挥的每一台机器：
       - description 说明这台机器是干嘛的
       - capabilities 是它能接的活（下发任务时用这个名字）
       - capabilities_detail 是每个能力的中文解释
       - input_files_supported 为 true 的节点已支持随任务下发文件（旧名 input_images_supported）
       - recent_tasks 是它最近的任务，按 updated_at 时间倒序（每条给 task_id/status/updated_at），
         用 recent_limit 控制每台机取几条（默认 10）。看细节再用 get_task(task_id)。
       另外还给出正在跑的任务数量和今日完成数。"""
    return db.digest(server_bundle=SERVER_BUNDLE, recent_limit=recent_limit)


@mcp.tool(meta={"openai/fileParams": ["files"]})
def submit_task(capability: str, goal: str, constraints: list | None = None,
                acceptance: list | None = None, deadline: str = "",
                priority: str = "normal",
                files: list[OpenAIFileParam] = Field(default_factory=list),
                input_files: list[dict] | None = None,
                input_images: list[dict] | None = None) -> dict:
    """下发一个任务。capability 从 get_digest 的节点列表里挑，不要指定机器名。
    goal 写清楚要什么、不要写步骤；acceptance 写能机器判定的验收条件。

    随任务带文件有两条路，按情况选一条即可：

    1) files —— 用户在 ChatGPT 里上传的附件（MP3/MP4/PDF/图片/表格…）。
       把附件放进 files 数组，服务端会自己从 download_url 取回原始字节，
       模型不需要、也不应该把文件内容读进上下文。
    2) input_files —— 调用方已经拿到字节时用，每项 {name, mime_type, data_base64}
       （也接受完整 data:...;base64,... data URL）。

    两条路共用同一套校验与落盘：图片必须文件头与声明一致；其它类型原样保留。
    节点会在任务工作目录的 inputs/ 下收到文件，执行提示里列出路径与 sha256。
    最多 8 个文件，单个 ≤10 MiB，合计 ≤15 MiB（服务端可调）。

    input_images 是历史参数名，等价于 input_files。"""
    merged = list(files or []) + list(input_files if input_files is not None
                                     else (input_images or []))
    try:
        prepared = _prepare_input_files(merged)
    except ValueError as exc:
        # 关键：不转成 ToolError 的话，SDK 会把异常当"崩溃"，模型只收到
        # 「Error executing tool submit_task」——真实原因（比如哪个域名被拦、
        # 哪个字段不对）全留在服务器日志里，调用方只能看到 INVALID_ARGUMENT。
        raise ToolError(f"文件接收失败：{exc}") from exc
    return db.submit_task(capability, goal, constraints, acceptance,
                          deadline or None, priority, created_by="chatgpt",
                          input_files=prepared)


@mcp.tool(meta={"openai/fileParams": ["files"]})
def probe_file_upload(files: list[OpenAIFileParam]) -> dict:
    """文件接收自检：把 ChatGPT 里上传的附件真正收下来，报告字节数与 SHA-256。

    用途只是验证"附件能不能交到服务端"。真实干活请用 submit_task 的 files 参数。
    调用方式：用户上传文件后，把附件放进 files 参数直接调用，不需要读文件内容。
    返回每个文件的 name / mime_type / bytes / sha256 / 落盘路径片段，可用来
    和本地源文件比对（SHA-256 相同即字节完全一致）。"""
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
    """取消任务；节点会终止对应的本地执行进程组。"""
    return db.close_task(task_id, reason, by="chatgpt")


@mcp.tool()
def get_task(task_id: str, artifact: str = "", offset: int = 0, limit: int = 8000) -> dict:
    """查一个任务：状态、约束、验收条件，以及它下面的进度和最终报告。

    读报告就看 reports[].payload：
      - payload.report         验收汇报正文（大活做成什么、每件小事各自如何）
      - payload.summary        一行结论
      - payload._acceptance_check  逐条验收的覆盖度；missing 非空说明哪几条没被回答
      - payload.artifacts      产物清单（含未上传的本地路径/大小/行数/schema）

    artifact 传产物名（见返回的 artifacts[].name）时，只返回该产物的一段（默认 8000 字节），
    翻页用 offset；图片产物用 get_artifact。
    input_files[]（旧别名 input_images[]）是随任务下发的输入文件元数据，不含字节；
    要看内容用 get_input_file(task_id, name)。"""
    view = db.task_view(task_id) or {"error": "not_found", "task_id": task_id}
    if artifact and "error" not in view:
        view["artifact_chunk"] = _artifact_chunk(task_id, artifact, offset, limit)
    return view


@mcp.tool()
def get_artifact(task_id: str, name: str = "", offset: int = 0, limit: int = 8000) -> dict | Image:
    """打开一个任务的产物看正文（"开箱看货"）。get_task 告诉你有什么，这个接口把货打开。

    产物名 name 从 get_task 返回的 artifacts[].name 取；留空则只列出该任务有哪些产物，
    不返回正文（name 写错时也会把可用名字列出来）。

    图片产物（image/png、image/jpeg、image/gif、image/webp）直接返回原生图片内容
    （MCP ImageContent），上层可视觉读取，不分页。

    文本产物按字节分页：返回 content 是这一段的文本，next_offset 非空就带上下一个 offset 继续翻，
    offset 从 0 开始。默认每次 8000 字节，上限 200000。整包原始数据不搬进上下文，要看哪段取哪段。"""
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
        # 图片走原生 content block；二进制分片无意义，decode 出来只会是乱码。
        return Image(data=blob["data"], format=fmt)
    return db.read_artifact(task_id, name, offset, limit)


@mcp.tool()
def get_input_file(task_id: str, name: str = "", offset: int = 0, limit: int = 8000):
    """回看一个任务随件下发的输入文件（get_task 的 input_files[] 里有什么，这里打开）。

    name 留空只列该任务有哪些输入文件（name/mime_type/bytes/sha256）。
    图片返回原生图片内容（MCP ImageContent），可直接视觉读取，不分页。
    文本类（text/*、json、csv、md、srt…）按字节分页返回 content + next_offset。
    其它二进制（pdf/zip/mp4…）不分页，返回元数据 + 提示：要就交给节点处理，
    不要把二进制塞进上下文。"""
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
    """把一个 PAD 已登记的产物换成短期签名的 HTTPS 交付链接（给人点开/下载用）。

    返回 pad.artifact/v2 契约，其中：
      - delivery.open_url      浏览器内联打开/播放
      - delivery.download_url  强制另存为文件
      - delivery.sha256 / size / mime_type  校验用
    零注册：不用预先建 *.artifact.json，也不会新建 worker 任务。task_id / name 从
    get_task 返回的 artifacts[].name 取。PAD 服务端内部调用 Artifact Bridge 完成
    「确认已登记 → 惰性物化 → 签名」，Bridge 密钥不经过本工具，也不会返回给调用方。

    ttl_sec 可选，签名有效期秒数（Bridge 默认 3600）。"""
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
    """把一个 PAD 已登记的产物取回成「模型可直接观察」的原生内容（一次调用，按需）。

    图片产物（image/*）返回原生 MCP 图片内容，可直接视觉读取。
    视频产物（video/*）返回从视频真实字节按时间点解码出的若干帧原生图片内容
    （每帧前带 t=…s 文本），外加一个指向完整文件的 resource_link；frames 控制抽
    几帧（默认服务端定）。
    其它文件返回 resource_link（小文件另附内嵌内容）。

    task_id / name 从 get_task 返回的 artifacts[].name 取。零注册：不用预先建
    *.artifact.json，也不会新建 worker 任务。PAD 服务端内部把请求转给产物观察面
    完成「确认已登记 → 惰性物化 → 解码抽帧」，内部密钥不会经过本工具、也不会返回。

    ttl_sec 可选，返回的完整文件链接有效期秒数。"""
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


# ---- LD 侧 -----------------------------------------------------------------

@app.post("/pad/{key}/api/register")
async def api_register(payload: dict):
    meta = payload.get("meta") or {}
    meta["bundle_hash"] = payload.get("bundle_hash", "")
    out = db.register_node(payload["node_id"], payload.get("role", ""),
                           payload.get("capabilities", []), meta,
                           payload.get("description", ""))
    out["server_bundle_hash"] = SERVER_BUNDLE       # 节点拿去自己比对
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


# ---- 管理侧（padctl / 脑的降级通道）----------------------------------------

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
    """供领取该任务的节点下载随任务提交的输入文件原始字节（图片/PDF/CSV/zip…）。"""
    out = db.read_task_input_file(task_id, name)
    if "error" in out:
        return JSONResponse(out, status_code=404 if out["error"] == "not_found" else 500)
    meta = out["meta"]
    return Response(out["data"], media_type=meta["mime_type"], headers={
        # 头只允许 latin-1，中文名必须走 RFC 5987 的 filename*，否则带中文的文件下载直接 500。
        "Content-Disposition": _attachment_disposition(meta["name"]),
        "X-PAD-SHA256": meta["sha256"],
        "Cache-Control": "no-store",
    })


@app.get("/pad/{key}/api/task/{task_id}/status")
async def api_task_status(task_id: str):
    """轻量状态：节点执行体在跑的时候轮询它，发现 cancelled 就终止本地进程组。"""
    st = db.task_status(task_id)
    return st or {"error": "not_found", "task_id": task_id}


@app.get("/pad/{key}/api/task/{task_id}/artifact")
async def api_task_artifact(task_id: str, name: str = "", offset: int = 0, limit: int = 8000):
    """取产物正文。name 留空只列产物；给 name 就按 offset/limit 取一段（同 MCP get_artifact）。"""
    view = db.task_view(task_id)
    if view is None:
        return {"error": "not_found", "task_id": task_id}
    if not name:
        return {"task_id": task_id, "artifacts": view["artifacts"]}
    return db.read_artifact(task_id, name, offset, limit)


@app.post("/pad/{key}/api/artifact")
async def api_artifact(request: Request, task_id: str, name: str, node_id: str = "",
                       rows: str = "", schema: str = "", local_path: str = ""):
    """节点把重要产物存一份上来（原始字节）。超限就拒，让它留在本地只回元数据。"""
    data = await request.body()
    return db.save_artifact(task_id, node_id, name, data, rows=rows, schema=schema,
                            local_path=local_path)



@app.post("/pad/{key}/api/task/{task_id}/close")
async def api_close_task(task_id: str, payload: dict):
    return db.close_task(task_id, payload.get("reason", ""), payload.get("by", "human"))


@app.get("/pad/{key}/join.md")
async def api_join_md():
    """给新机器上的 agent 看的接入协议。"""
    doc = (HERE / "JOIN.md")
    return PlainTextResponse(doc.read_text() if doc.exists() else "JOIN.md missing")


@app.get("/pad/{key}/agent.bundle.tgz")
async def api_agent_bundle():
    """客户端骨架：新机器解包就能跑。"""
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
