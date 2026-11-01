#!/usr/bin/env python3
"""生成 README 里的演示图（真实终端输出 → HTML → Chrome 截图）。

不手工拼图：每张图里的文字都是本机真跑出来的输出，改代码后重跑这个脚本即可刷新。

    .venv/bin/python docs/make_demo_images.py

依赖：真 Chrome 渲染。默认用仓库外的 `tools/webshot/webshot.py`（我的通用截图工具），
没有就用 WEBSHOT=/path/to/webshot.py 指一个，缺了会把 HTML 留在 docs/img/ 待手截。
还需要一个跑着的本地 PAD（默认 8790，可用 PORT= 换）。
"""
import html
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
WS = Path(os.environ.get("WEBSHOT", ROOT.parent / "tools" / "webshot" / "webshot.py"))
WS_PY = os.environ.get("WEBSHOT_PYTHON") or "python3"   # webshot 要 websocket-client，用系统 python 更稳
OUT = HERE / "img"
PORT = os.environ.get("PORT", "8790")
KEY = (ROOT / "data" / ".pad_key").read_text().strip() if (ROOT / "data" / ".pad_key").exists() else ""
BASE = f"http://127.0.0.1:{PORT}/pad/{KEY}"

CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body {
  margin: 0; padding: 28px 30px; background: #0d1117;
  font: 13px/1.65 "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  color: #c9d1d9;
}
.card { background: #161b22; border: 1px solid #30363d; border-radius: 10px;
        overflow: hidden; box-shadow: 0 12px 32px rgba(0,0,0,.45); }
.bar { display: flex; align-items: center; gap: 8px; padding: 10px 14px;
       background: #21262d; border-bottom: 1px solid #30363d; }
.dot { width: 11px; height: 11px; border-radius: 50%; }
.title { margin-left: 8px; color: #8b949e; font-size: 12px; }
.body { padding: 16px 18px; white-space: pre-wrap; word-break: break-word; }
.p { color: #58a6ff; }     /* 提示符 */
.g { color: #3fb950; }     /* 成功 */
.y { color: #d29922; }     /* 强调 */
.d { color: #6e7681; }     /* 次要 */
.w { color: #f0f6fc; font-weight: 600; }
h1 { font: 600 15px/1.4 -apple-system, "Segoe UI", sans-serif; color: #f0f6fc;
     margin: 0 0 14px; letter-spacing: .2px; }
.tag { display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11px;
       background: #1f6feb22; color: #58a6ff; border: 1px solid #1f6feb55; margin-left: 8px; }
"""


def render(name: str, title: str, lines: str, width: int = 900) -> Path:
    """lines 已经带好 span 标记；写成 HTML 再交给 webshot 截图。

    没有 webshot 时不报错 —— 把 HTML 留在 docs/img/<name>.html，
    自己用浏览器打开截一张就行（图里的内容一样，只是少了自动化）。
    """
    page = HERE / f".demo-{name}.html"
    page.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<style>{CSS}</style></head><body>
<div style="width:{width}px">
  <h1>{html.escape(title)}</h1>
  <div class="card">
    <div class="bar">
      <span class="dot" style="background:#ff5f57"></span>
      <span class="dot" style="background:#febc2e"></span>
      <span class="dot" style="background:#28c840"></span>
      <span class="title">pad</span>
    </div>
    <div class="body">{lines}</div>
  </div>
</div></body></html>""")
    OUT.mkdir(parents=True, exist_ok=True)
    out = OUT / f"{name}.png"
    if not WS.exists():
        kept = OUT / f"{name}.html"
        page.replace(kept)
        print(f"{kept.relative_to(ROOT)}  ← 没有 {WS}，HTML 留着待手截")
        return kept
    subprocess.run([WS_PY, str(WS), "--url", page.as_uri(), "--out", str(out),
                    "--selector", "body", "--width", str(width + 60), "--height", "1200",
                    "--scale", "2", "--pad", "0", "--wait", "1"],
                   check=True, capture_output=True)
    page.unlink()
    print(f"{out.relative_to(ROOT)}")
    return out


def sh(cmd: list[str]) -> str:
    env = dict(os.environ, PAD_URL=BASE)
    r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env=env)
    out = (r.stdout + r.stderr).strip()
    if r.returncode != 0 or "Traceback" in out:
        sys.exit(f"演示命令失败（先确认 PAD 跑在 {BASE}）：\n$ {' '.join(cmd)}\n{out[:2000]}")
    return out


def mask(s: str) -> str:
    s = s.replace(KEY, "<KEY>") if KEY else s
    return s.replace(socket.gethostname(), "my-machine").replace(
        socket.gethostname().split(".")[0], "my-machine")


# ---------- 1. quickstart 真跑输出 ----------
def demo_quickstart():
    log = ROOT / "data" / "quickstart.log"
    lines = []
    if log.exists():
        ok = [l for l in log.read_text(errors="ignore").splitlines()
              if l.startswith("[") and "report:" in l][-3:]
        for l in ok:
            lv = "g" if "done" in l else "d"
            lines.append(f'<span class="{lv}">{html.escape(mask(l))}</span>')
    nodes = sh(["./.venv/bin/python", "padctl.py", "nodes"])
    try:
        ns = json.loads(nodes)
        for n in ns:
            lines.append(f'<span class="d">节点</span> '
                         f'<span class="w">{n["node_id"]}</span> '
                         f'<span class="d">{", ".join(n["capabilities"])}</span>')
    except Exception:
        pass
    body = (
        '<span class="p">$</span> ./quickstart.sh\n'
        '<span class="g">▸</span> 装依赖（requirements.txt）\n'
        '<span class="g">▸</span> 生成访问密钥（data/.pad_key，权限 600）\n'
        '<span class="g">▸</span> 起 PAD 服务端 :8790（数据在 data）\n'
        '<span class="g">▸</span> 起本地节点 local-1（能力 demo.echo / local.run）\n'
        '<span class="g">▸</span> 下发任务 demo.echo，等节点领活并回报\n\n'
        + "\n".join(lines) + '\n\n'
        '<span class="g">✓</span> PAD 已经跑起来了 · <span class="d">MCP →</span> '
        f'<span class="y">{html.escape(mask(BASE))}/mcp</span>'
    )
    render("quickstart", "一键起步：装依赖 → 起服务 → 起节点 → 跑通第一个任务", body)


# ---------- 2. padctl digest ----------
def demo_digest():
    out = mask(sh(["./.venv/bin/python", "padctl.py", "digest"]))
    lines = []
    for l in out.splitlines():
        if l.startswith("时间"):
            lines.append(f'<span class="d">{html.escape(l)}</span>')
        elif l.strip().startswith("caps:"):
            lines.append(f'<span class="d">{html.escape(l)}</span>')
        elif l.startswith("节点") or l.startswith("在跑") or l.startswith("今日"):
            lines.append(f'<span class="y">{html.escape(l)}</span>')
        elif l.strip().startswith("local-1") or l.strip().startswith("mac-1") \
                or l.strip().startswith("vps-1") or l.strip().startswith("gpu-1"):
            lines.append(f'<span class="w">{html.escape(l)}</span>')
        else:
            lines.append(html.escape(l))
    render("digest", "./padctl.py digest —— 谁在线、在跑什么、今天干完了多少",
           "\n".join(lines))


# ---------- 3. MCP 工具面 ----------
def demo_mcp():
    tools = sh(["./.venv/bin/python", "pad_mcp.py", "tools"])
    lines = [f'<span class="d">{html.escape(mask(tools))}</span>']
    body = (
        '<span class="p">$</span> ./pad_mcp.py tools '
        '<span class="d"># 大脑（ChatGPT / Claude）看到的就是这 9 个</span>\n'
        + "\n".join(lines) + '\n\n'
        '<span class="p">$</span> ./pad_mcp.py submit --capability demo.echo \\\n'
        '    --goal "自检：回一句 pong" --acceptance "返回 pong"\n'
        '<span class="g">{"task_id": "t-20261102-…", "status": "queued"}</span>\n\n'
        '<span class="p">$</span> ./pad_mcp.py task t-20261102-…\n'
        '<span class="w">progress</span>  开始执行 demo.echo\n'
        '<span class="g">done</span>      pong from my-machine at 05:33'
    )
    render("mcp", "MCP 工具面 —— 只给大脑 9 个工具，没有 shell、没有机器名", body)


# ---------- 4. 一张图的架构 ----------
def demo_flow():
    body = """<span class="y">大脑（ChatGPT / Claude）</span>
  │  <span class="d">submit_task(capability, goal, constraints, acceptance, files)</span>
  ▼
<span class="w">PAD</span>  <span class="d">一个进程 · 一个 SQLite · 两扇门</span>
  │
  ├─ <span class="p">/pad/&lt;KEY&gt;/mcp</span>          <span class="d">大脑看到的：9 个任务/报告工具</span>
  └─ <span class="p">/pad/&lt;KEY&gt;/api/…</span>        <span class="d">节点用的：register / claim / report</span>

  │  <span class="d">按 capability 派活，不按机器派</span>
  ▼
<span class="w">节点（任意多台）</span>
  ├─ <span class="d">local-1</span>   demo.echo, local.run
  ├─ <span class="d">gpu-1</span>     build.heavy  <span class="y">concurrency: 2</span>
  └─ <span class="d">vps-1</span>     scrape.batch

<span class="d">每个任务：</span>claim → 本机执行体（opencode / claude / codex / 一条命令）
          → result.json（done / failed + 逐条验收证据）→ 汇报
<span class="d">模型无状态，PAD 有状态。</span>"""
    render("flow", "PAD 是什么：一个 CPU 调度器，只不过调度的对象是\"任务\"和\"机器\"", body, 860)


if __name__ == "__main__":
    if not KEY:
        sys.exit("先跑 ./quickstart.sh，生成本地密钥与数据目录")
    demo_flow()
    demo_quickstart()
    demo_digest()
    demo_mcp()
