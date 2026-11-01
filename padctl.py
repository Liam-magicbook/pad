#!/usr/bin/env python3
"""PAD 本地管理 CLI（人用，也可以当脑的降级通道）。

    export PAD_URL=http://127.0.0.1:8790/pad/<KEY>
    ./padctl.py digest
    ./padctl.py submit --capability demo.echo --goal "自检"
"""
import argparse
import json
import os
import sys
import time

import requests

BASE = (os.environ.get("PAD_URL") or "").rstrip("/")
H = {"Content-Type": "application/json; charset=utf-8"}


def call(method, path, payload=None, **params):
    if not BASE:
        sys.exit("请先 export PAD_URL=https://<host>/pad/<KEY>")
    r = requests.request(method, f"{BASE}/api/{path}", json=payload, params=params,
                         headers=H, timeout=30)
    r.raise_for_status()
    return r.json()


def show(d):
    print(json.dumps(d, ensure_ascii=False, indent=2))


def cmd_digest(_):
    d = call("GET", "digest")
    print(f"时间 {d['now']}")
    print("\n节点:")
    for n in d["nodes"]:
        flag = "  [失联]" if n["node_id"] in d["nodes_stale"] else ""
        print(f"  {n['node_id']:<12} {n['summary'] or '-':<28} {n['last_seen']}{flag}")
        print(f"    caps: {', '.join(n['capabilities'])}")
    print(f"\n在跑任务 ({len(d['active_tasks'])}):")
    for t in d["active_tasks"]:
        print(f"  {t['task_id']}  {t['status']:<9} {t['capability']:<16} {t['goal'][:40]}")
    print(f"\n今日完成: {d['done_today']}")


def cmd_task(a):
    show(call("GET", f"task/{a.task_id}"))


def _file_payload(paths):
    """把本地文件转成 PAD 的 input_files 结构（base64 + MIME + 原名）。"""
    import base64
    import mimetypes
    from pathlib import Path

    out = []
    for p in paths:
        f = Path(p).expanduser()
        if not f.is_file():
            sys.exit(f"文件不存在: {p}")
        data = f.read_bytes()
        mime = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        out.append({"name": f.name, "mime_type": mime,
                    "data_base64": base64.b64encode(data).decode()})
    return out


def cmd_submit(a):
    payload = {
        "capability": a.capability, "goal": a.goal,
        "constraints": a.constraint or [], "acceptance": a.acceptance or [],
        "priority": a.priority, "created_by": "human",
    }
    if getattr(a, "file", None):
        payload["input_files"] = _file_payload(a.file)
    show(call("POST", "task", payload))


def cmd_inputs(a):
    show({"task_id": a.task_id,
          "input_files": call("GET", f"task/{a.task_id}").get("input_files", [])})


def cmd_input(a):
    """把任务的输入文件下载到本地（保留原名）。"""
    import requests
    from pathlib import Path

    dest = Path(a.out or ".").expanduser()
    dest.mkdir(parents=True, exist_ok=True)
    if a.name:
        names = [a.name]
    else:
        names = [x["name"] for x in call("GET", f"task/{a.task_id}").get("input_files", [])]
    if not names:
        sys.exit("该任务没有输入文件")
    for name in names:
        r = requests.get(f"{BASE}/api/task/{a.task_id}/input-file",
                         params={"name": name}, timeout=120, stream=True)
        r.raise_for_status()
        target = dest / name
        with target.open("wb") as fh:
            for chunk in r.iter_content(1 << 16):
                fh.write(chunk)
        print(f"{target}  {target.stat().st_size} bytes  "
              f"sha256={r.headers.get('X-PAD-SHA256', '')[:12]}")


def cmd_watch(a):
    while True:
        d = call("GET", "digest")
        print(f"[{d['now']}] 在跑 {len(d['active_tasks'])} | "
              f"今日完成 {d['done_today']} | 失联 {d['nodes_stale'] or '无'}")
        time.sleep(a.interval)


if __name__ == "__main__":
    p = argparse.ArgumentParser(prog="padctl")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("digest").set_defaults(fn=cmd_digest)
    sub.add_parser("nodes").set_defaults(fn=lambda a: show(call("GET", "nodes")))
    f = sub.add_parser("forget"); f.add_argument("node_id")
    f.set_defaults(fn=lambda a: show(call("POST", f"node/{a.node_id}/forget")))

    t = sub.add_parser("task"); t.add_argument("task_id"); t.set_defaults(fn=cmd_task)

    s = sub.add_parser("submit")
    s.add_argument("--capability", required=True)
    s.add_argument("--goal", required=True)
    s.add_argument("--constraint", action="append")
    s.add_argument("--acceptance", action="append")
    s.add_argument("--priority", default="normal")
    s.add_argument("--file", action="append", metavar="PATH",
                   help="随任务下发的文件，可重复（图片/PDF/表格/压缩包/音视频都行）")
    s.set_defaults(fn=cmd_submit)

    ins = sub.add_parser("inputs", help="列出一个任务的输入文件")
    ins.add_argument("task_id")
    ins.set_defaults(fn=cmd_inputs)

    in1 = sub.add_parser("input", help="把一个任务的输入文件下载到本地")
    in1.add_argument("task_id")
    in1.add_argument("--name", default="", help="只取一个（默认全取）")
    in1.add_argument("--out", default="", help="落盘目录（默认当前目录）")
    in1.set_defaults(fn=cmd_input)

    cl = sub.add_parser("close"); cl.add_argument("task_id"); cl.add_argument("--reason", default="")
    cl.set_defaults(fn=lambda a: show(call("POST", f"task/{a.task_id}/close", {"reason": a.reason})))

    w = sub.add_parser("watch"); w.add_argument("--interval", type=int, default=15)
    w.set_defaults(fn=cmd_watch)

    args = p.parse_args()
    args.fn(args)
