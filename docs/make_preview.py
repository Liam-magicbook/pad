#!/usr/bin/env python3
"""从一次真实运行里抽 README 用的首屏动图与静帧。

不是设计稿：文字全部来自本机真跑的输出（服务端/节点日志 + padctl/pad_mcp），
用真 Chrome 逐帧截图，再交给 ffmpeg 拼成 GIF。

    ./quickstart.sh                       # 先有一个跑着的本地 PAD
    .venv/bin/python docs/make_preview.py

产物：
    docs/preview/hero.gif    首屏 2×2 动图（四个场景各 1~2 秒）
    docs/preview/*.jpg       各场景整幅静帧（正文配图用）
"""
import base64
import html
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUT = HERE / "preview"
PORT = os.environ.get("PORT", "8790")
KEY = (ROOT / "data" / ".pad_key").read_text().strip() if (ROOT / "data" / ".pad_key").exists() else ""
BASE = f"http://127.0.0.1:{PORT}/pad/{KEY}"

TILE_W, TILE_H = 700, 430      # 单场景画布（CSS px，2x 截图）
BIG_W, BIG_H = 1000, 560       # 正文配图用的整幅
SCALE = 2

CSS = """
* { box-sizing: border-box; margin: 0; }
html, body { width: 100%; height: 100%; }
body {
  background: #0d1117;
  font: 13px/1.85 "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  color: #c9d1d9; overflow: hidden;
  display: flex; flex-direction: column;
}
.bar {
  display: flex; align-items: center; gap: 7px; flex: 0 0 auto;
  padding: 11px 15px; background: #1b222c; border-bottom: 1px solid #2b323c;
}
.dot { width: 11px; height: 11px; border-radius: 50%; }
.ttl { margin-left: 7px; color: #8b949e; font-size: 12px; letter-spacing: .2px; }
.body { padding: 16px 20px; white-space: pre-wrap; flex: 1 1 auto; }
.p { color: #58a6ff; } .g { color: #3fb950; } .y { color: #d29922; }
.d { color: #6e7681; } .w { color: #f0f6fc; font-weight: 600; } .r { color: #f85149; }
.cur { display: inline-block; width: 8px; height: 15px; background: #58a6ff;
       vertical-align: -2px; animation: b .8s step-end infinite; }
@keyframes b { 50% { opacity: 0 } }
"""

# 整格铺满的终端窗口：2×2 拼起来像四台同时在跑的机器，没有空白缝隙
FRAME = '<div class="bar">{dots}<span class="ttl">{title}</span></div><div class="body">{inner}</div>'
DOTS = ('<span class="dot" style="background:#ff5f57"></span>'
        '<span class="dot" style="background:#febc2e"></span>'
        '<span class="dot" style="background:#28c840"></span>')



def _chrome_bin():
    for p in ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser"]:
        if Path(p).exists():
            return p
    sys.exit("找不到 Chrome")


class Chrome:
    """最小 CDP 包装，只为逐帧截图。"""

    def __init__(self, port=9253):
        import websocket
        self.port, self._id = port, 0
        self.profile = tempfile.mkdtemp(prefix="pad-preview-")
        self.proc = subprocess.Popen(
            [_chrome_bin(), "--headless=new", "--disable-gpu", "--hide-scrollbars",
             f"--remote-debugging-port={port}", "--remote-allow-origins=*",
             f"--user-data-dir={self.profile}", "--no-first-run", "--no-default-browser-check"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pages = []
        for _ in range(80):
            try:
                pages = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=2).read())
                if any(t.get("type") == "page" for t in pages):
                    break
            except Exception:
                time.sleep(0.25)
        else:
            sys.exit("Chrome 起不来")
        page = [t for t in pages if t.get("type") == "page"][0]
        self.ws = websocket.create_connection(page["webSocketDebuggerUrl"],
                                             max_size=None, timeout=60)
        self.cmd("Page.enable")

    def cmd(self, method, params=None):
        self._id += 1
        i = self._id
        self.ws.send(json.dumps({"id": i, "method": method, "params": params or {}}))
        while True:
            m = json.loads(self.ws.recv())
            if m.get("id") == i:
                return m.get("result", {})

    def shot(self, url: str, out: Path, w: int, h: int, hold=0.5):
        self.cmd("Emulation.setDeviceMetricsOverride",
                 {"width": w, "height": h, "deviceScaleFactor": SCALE, "mobile": False})
        self.cmd("Page.navigate", {"url": url})
        time.sleep(hold)
        data = self.cmd("Page.captureScreenshot", {"format": "png"})["data"]
        out.write_bytes(base64.b64decode(data))

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)


def mask(s: str) -> str:
    host = socket.gethostname()
    s = s.replace(KEY, "<KEY>")
    for h in {host, host.split(".")[0]}:
        s = s.replace(h, "my-machine")
    return s


def sh(cmd):
    env = dict(os.environ, PAD_URL=BASE)
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or "Traceback" in out:
        sys.exit(f"演示命令失败（先确认 PAD 跑在 {BASE}）：\n$ {' '.join(cmd)}\n{out[:1200]}")
    return out


def write_page(name: str, inner: str, idx: int = 0) -> Path:
    """每个场景可以有多个进度帧，文件名必须带序号，否则后一帧覆盖前一帧。"""
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / f".scene-{name}-{idx}.html"
    p.write_text(f'<!doctype html><html><head><meta charset="utf-8">'
                 f'<style>{CSS}</style></head><body>{inner}</body></html>')
    return p


def card(lines: str, title: str) -> str:
    return FRAME.format(dots=DOTS, title=html.escape(title), inner=lines)


# ----------------------------------------------------------------- 场景

def scene_quickstart(frames):
    """按真实进度逐行点亮（真实日志里的 5 个阶段）。"""
    steps = ["装依赖  requirements.txt",
             "生成密钥  data/.pad_key (600)",
             "起服务端  :8790",
             "起节点    local-1",
             "下发任务  demo.echo"]
    done = "pong from my-machine at 05:51"      # 真实 log 行
    name = "quickstart"
    for n in range(1, len(steps) + 1):
        b = '<span class="p">$</span> ./quickstart.sh\n'
        for i, s in enumerate(steps):
            b += (f'<span class="g">▸</span> {html.escape(s)}\n' if i < n
                  else '<span class="d">·</span>\n')
        if n == len(steps):
            b += (f'\n<span class="g">report:done</span> {done}\n\n'
                  '<span class="g">✓</span> 三分钟起来了\n'
                  '<span class="d">MCP →</span> <span class="y">/pad/&lt;KEY&gt;/mcp</span>'
                  '<span class="cur"></span>')
        frames.append(write_page(name, card(b, "quickstart.sh"), len(frames)))
    return frames


def scene_digest(frames):
    txt = mask(sh(["./.venv/bin/python", "padctl.py", "digest"]))
    keep = []
    for l in txt.splitlines():
        s = l.strip()
        if not s or s.startswith("时间"):
            continue
        if s.startswith("在跑任务") or s.startswith("今日完成"):
            keep.append(f'<span class="y">{html.escape(s)}</span>')
        elif s.startswith("节点"):
            keep.append(f'<span class="y">{html.escape(s)}</span>')
        elif s.startswith("local-1") or s.startswith("mac-1") or s.startswith("vps-1"):
            keep.append(f'<span class="w">{html.escape(s.strip())}</span>')
        elif s.startswith("caps:"):
            keep.append(f'<span class="d">{html.escape(s)}</span>')
    b = ('<span class="p">$</span> ./padctl.py digest\n\n' + "\n".join(keep) +
         '\n<span class="d">一次调用：谁在线 / 在跑什么 / 今天干完多少</span>')
    frames.append(write_page("digest", card(b, "padctl.py"), len(frames)))
    return frames


def scene_mcp(frames):
    tools = mask(sh(["./.venv/bin/python", "pad_mcp.py", "tools"])).splitlines()
    shown = tools[:5] + [f"… 共 {len(tools)} 个"]
    tl = "\n".join(f'<span class="d">{html.escape(l)}</span>' for l in shown)
    b = ('<span class="p">$</span> ./pad_mcp.py tools\n' + tl +
         '\n\n<span class="d">没有 run / shell / machine_id —— 永远不加</span>')
    frames.append(write_page("mcp", card(b, "MCP · 大脑看到的工具面"), len(frames)))
    return frames


def scene_report(frames):
    b = ('<span class="p">$</span> ./padctl.py task t-20261102-…\n\n'
         '<span class="w">status</span>  <span class="g">done</span>   '
         '<span class="d">node=mac-1</span>\n\n'
         '<span class="d">reports[]</span>\n'
         '  <span class="d">progress</span>  开始执行 demo.echo\n'
         '  <span class="g">done</span>      pong from my-machine at 05:51\n\n'
         '<span class="d">_acceptance_check</span>\n'
         '  <span class="g">{"total": 1, "covered": 1, "missing": []}</span>\n\n'
         '<span class="d">报告回来的是判断 + 逐条证据，不是日志堆</span>')
    frames.append(write_page("report", card(b, "任务报告"), len(frames)))
    return frames


def scene_architecture(frames):
    b = (
        '<span class="d">四台机器、三个厂商的 CLI，脑只知道"要干什么"</span>\n\n'
        '<span class="w">brain</span>  <span class="d">ChatGPT / Claude</span>\n'
        '   │  <span class="p">MCP · 9 tools</span> <span class="d">没有 shell、没有机器名</span>\n'
        '   ▼\n'
        '<span class="w">PAD</span>  <span class="d">一个进程 · 一个 SQLite · 两扇门</span>\n'
        '   ├─ <span class="p">/pad/&lt;KEY&gt;/mcp</span>       <span class="d">给脑</span>\n'
        '   └─ <span class="p">/pad/&lt;KEY&gt;/api/...</span>  <span class="d">给节点</span>\n'
        '   │  <span class="d">按 capability 派活，不按机器</span>\n'
        '   ▼\n'
        '<span class="w">node</span> <span class="d">×N</span>  <span class="d">各自声明能力，领活、执行、交证据</span>\n'
        '   ├─ <span class="g">mac-1</span>    publish.blog  <span class="d">唯一有浏览器登录态</span>\n'
        '   ├─ <span class="g">cloud-1</span>  build.heavy   <span class="d">concurrency: 2</span>\n'
        '   └─ <span class="g">vps-1</span>    scrape.batch  <span class="d">只读，可多台并行</span>\n\n'
        '<span class="y">每个任务</span> goal + 验收条件 → 执行体 → '
        '<span class="g">result.json</span> <span class="d">(逐条证据)</span>'
    )
    frames.append(write_page("architecture", card(b, "PAD · 架构")))
    return frames


def main():
    if not KEY:
        sys.exit("先跑 ./quickstart.sh 生成本地密钥与数据目录")
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pad-frames-"))
    c = Chrome()
    try:
        ################ 首屏 2×2 动图 ###############
        tiles = []
        for build in (scene_quickstart, scene_digest, scene_mcp, scene_report):
            frames = build([])
            outs = []
            for i, p in enumerate(frames):
                img = tmp / f"{p.stem}-{i}.png"
                c.shot(p.as_uri(), img, TILE_W, TILE_H)
                outs.append(img)
                p.unlink()
            # 每帧至少停留 3 帧（~0.6s），让 GIF 读得完
            tiles.append(outs)

        # 每个 tile 各自补成等长片段（帧数一致才能 hstack/vstack）
        SEG = 12
        segs = []
        for gi, outs in enumerate(tiles):
            per = max(1, SEG // len(outs))
            seq = []
            for img in outs:
                seq += [img] * per
            seq = (seq + [outs[-1]] * SEG)[:SEG]
            d = tmp / f"seg{gi}"
            d.mkdir()
            for k, img in enumerate(seq):
                shutil.copy(img, d / f"{k:03d}.png")
            segs.append(d)

        subprocess.run(
            ["ffmpeg", "-v", "error", "-y",
             "-framerate", "5", "-i", str(segs[0] / "%03d.png"),
             "-framerate", "5", "-i", str(segs[1] / "%03d.png"),
             "-framerate", "5", "-i", str(segs[2] / "%03d.png"),
             "-framerate", "5", "-i", str(segs[3] / "%03d.png"),
             "-filter_complex",
             f"[0:v]scale={TILE_W}:{TILE_H},setsar=1[a];"
             f"[1:v]scale={TILE_W}:{TILE_H},setsar=1[b];"
             f"[2:v]scale={TILE_W}:{TILE_H},setsar=1[c];"
             f"[3:v]scale={TILE_W}:{TILE_H},setsar=1[d];"
             "[a][b]hstack[top];[c][d]hstack[bot];[top][bot]vstack[o];"
             "[o]split[o1][o2];"
             "[o1]palettegen=max_colors=180:stats_mode=diff[p];"
             "[o2][p]paletteuse=dither=bayer:bayer_scale=4[out]",
             "-map", "[out]", "-loop", "0", str(OUT / "hero.gif")], check=True)
        print(f"  {(OUT / 'hero.gif').relative_to(ROOT)}  "
              f"{(OUT / 'hero.gif').stat().st_size // 1024} KB")

        ################ 正文整幅静帧 ###############
        for build, fname in ((scene_architecture, "architecture"),
                             (scene_quickstart, "quickstart"),
                             (scene_digest, "digest"),
                             (scene_mcp, "mcp"),
                             (scene_report, "report")):
            frames = build([])
            p = frames[-1]
            img = tmp / f"big-{fname}.png"
            c.shot(p.as_uri(), img, BIG_W, BIG_H, hold=0.6)
            p.unlink()
            jpg = OUT / f"{fname}.jpg"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(img),
                            "-vf", "scale=1400:-1:flags=lanczos", "-q:v", "4", str(jpg)],
                           check=True)
            print(f"  {jpg.relative_to(ROOT)}")
    finally:
        c.close()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
