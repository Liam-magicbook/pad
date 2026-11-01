#!/usr/bin/env python3
"""通过 MCP 端点用脑的那套工具（不是 REST 降级通道）。

为什么要有它：ChatGPT 走的就是 `/pad/<KEY>/mcp`，本地调试如果一直用 REST，
测出来的东西和脑子实际能用到的不是一回事（工具 schema、返回结构都不同）。

用法:
    export PAD_MCP_URL="http://127.0.0.1:8790/pad/$PAD_KEY/mcp"

    ./pad_mcp.py tools
    ./pad_mcp.py digest
    ./pad_mcp.py submit --capability demo.echo --goal "自检" --acceptance "回 pong"
    ./pad_mcp.py submit --capability my.agent --goal "..." --file a.pdf --file b.xlsx
    ./pad_mcp.py task t-20261026-xxxxxx
    ./pad_mcp.py input-file t-20261026-xxxxxx 报告.pdf --out /tmp
    ./pad_mcp.py call submit_task '{"capability":"x","goal":"y"}'
"""
import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def url() -> str:
    u = os.environ.get("PAD_MCP_URL", "").strip()
    if u:
        return u
    base = (os.environ.get("PAD_URL") or "").rstrip("/")
    if base:
        return f"{base}/mcp"
    key = os.environ.get("PAD_KEY") or ""
    if not key:
        kf = Path.home() / ".pad_key"
        key = kf.read_text().strip() if kf.exists() else ""
    if not key:
        sys.exit("请设置 PAD_MCP_URL 或 PAD_URL，或在 ~/.pad_key 放密钥")
    host = os.environ.get("PAD_HOST", "127.0.0.1:8790")
    scheme = os.environ.get("PAD_SCHEME", "http")
    return f"{scheme}://{host}/pad/{key}/mcp"


def _text(result) -> str:
    """MCP 结果里的文本块拼起来；图片块给一行摘要（不要把二进制打进终端）。"""
    out = []
    for c in getattr(result, "content", []) or []:
        t = getattr(c, "type", "")
        if t == "text":
            out.append(c.text)
        elif t == "image":
            n = len(getattr(c, "data", "") or "")
            out.append(f"[image {getattr(c, 'mime_type', '?')} ~{n * 3 // 4} bytes]")
        else:
            out.append(f"[{t}]")
    return "\n".join(out)


async def _run(fn):
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(180), follow_redirects=True) as hc:
        async with streamable_http_client(url(), http_client=hc) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as s:
                await s.initialize()
                return await fn(s)


def _files(paths):
    import mimetypes

    out = []
    for p in paths or []:
        f = Path(p).expanduser()
        if not f.is_file():
            sys.exit(f"文件不存在: {p}")
        data = f.read_bytes()
        out.append({"name": f.name,
                    "mime_type": mimetypes.guess_type(f.name)[0] or "application/octet-stream",
                    "data_base64": base64.b64encode(data).decode()})
    return out


def cmd_tools(_a):
    async def go(s):
        tools = await s.list_tools()
        for t in tools.tools:
            schema = getattr(t, "input_schema", None) or getattr(t, "inputSchema", None) or {}
            props = list((schema.get("properties") or {}).keys())
            print(f"- {t.name}({', '.join(props)})")
    asyncio.run(_run(go))


def cmd_call(a):
    args = json.loads(a.args) if a.args else {}

    async def go(s):
        res = await s.call_tool(a.tool, args)
        print(_text(res))
    asyncio.run(_run(go))


def cmd_digest(_a):
    async def go(s):
        res = await s.call_tool("get_digest", {})
        print(_text(res))
    asyncio.run(_run(go))


def cmd_task(a):
    async def go(s):
        res = await s.call_tool("get_task", {"task_id": a.task_id})
        print(_text(res))
    asyncio.run(_run(go))


def cmd_submit(a):
    args = {"capability": a.capability, "goal": a.goal, "priority": a.priority}
    if a.constraint:
        args["constraints"] = a.constraint
    if a.acceptance:
        args["acceptance"] = a.acceptance
    if a.deadline:
        args["deadline"] = a.deadline
    files = _files(a.file)
    if files:
        args["input_files"] = files

    async def go(s):
        res = await s.call_tool("submit_task", args)
        print(_text(res))
    asyncio.run(_run(go))


def cmd_input_file(a):
    dest = Path(a.out or ".").expanduser()
    dest.mkdir(parents=True, exist_ok=True)

    async def go(s):
        res = await s.call_tool("get_input_file", {"task_id": a.task_id, "name": a.name})
        for c in res.content:
            if getattr(c, "type", "") == "image":
                t = dest / (Path(a.name).stem + ".bin")
                t.write_bytes(base64.b64decode(c.data))
                print(f"{t}  {t.stat().st_size} bytes (image {c.mime_type})")
            else:
                print(getattr(c, "text", ""))
    asyncio.run(_run(go))


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="pad_mcp")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("tools").set_defaults(fn=cmd_tools)
    sub.add_parser("digest").set_defaults(fn=cmd_digest)

    s = sub.add_parser("submit")
    s.add_argument("--capability", required=True)
    s.add_argument("--goal", required=True)
    s.add_argument("--constraint", action="append")
    s.add_argument("--acceptance", action="append")
    s.add_argument("--deadline", default="")
    s.add_argument("--priority", default="normal")
    s.add_argument("--file", action="append", metavar="PATH")
    s.set_defaults(fn=cmd_submit)

    t = sub.add_parser("task"); t.add_argument("task_id"); t.set_defaults(fn=cmd_task)

    i = sub.add_parser("input-file")
    i.add_argument("task_id"); i.add_argument("name"); i.add_argument("--out", default="")
    i.set_defaults(fn=cmd_input_file)

    c = sub.add_parser("call"); c.add_argument("tool"); c.add_argument("args", nargs="?")
    c.set_defaults(fn=cmd_call)

    args = p.parse_args()
    args.fn(args)
