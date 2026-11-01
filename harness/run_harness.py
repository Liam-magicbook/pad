#!/usr/bin/env python3
"""把一个 PAD 任务翻译成「本机某个 agent CLI」的无头调用，并把结果收敛成一句话。

用法：
    run_harness.py [opencode|claude|codex]        # 默认 opencode

PAD 本身不关心你用什么执行体 —— 它只发"要干什么"。适配在这里，
每个节点自己决定。换执行体就是换这一个参数。

为了统一行为，三个执行体都按同一套约定收口：
  提示词从 harness/prompt.md.tmpl 渲染
  完成或无法完成时都写 $PAD_WORK_DIR/result.json，由退出码区分 done / failed
  完整输出留在 $PAD_WORK_DIR/run.log，永远不上传
  rc==0 却没写 result.json（执行体"提前收尾"）时，opencode v2 / codex 按同一会话再催一次；
  claude 没有可续的会话概念，跳过（PAD_RESUME_ONCE=0 可关掉续跑）。
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TMPL = HERE / "prompt.md.tmpl"

# rc==0 却没写 result.json 时，多半是执行体「提前收尾」（把长任务放会话后台就结束了本轮，
# 进程退出把后台一并带走）。用同一个会话再催一次，给一次真正做完的机会。
RESUME_PROMPT = """上一轮你把对话收尾了，但 PAD 要求的 ${PAD_WORK_DIR}/result.json 并没有写出来，
说明任务很可能没真正完成（常见原因：把长任务放到会话后台就结束了本轮，进程一退，后台连带被杀）。

不要重头再来。先只读地检查 ${PAD_WORK_DIR} 里已经产出的东西、以及有没有半途中断的产物，
判断还差哪些必需步骤；然后在**本轮内**把它们做完 —— 长任务要在这一轮里用 sleep/tail 轮询到
真的结束（不要再次抛下就收尾）。最后按契约写 ${PAD_WORK_DIR}/result.json。
如果穷尽合理路径仍无法完成，就写 ${PAD_WORK_DIR}/result.json，如实说明尝试、阻碍和未完成项。"""

# rc!=0、且是上游 provider 回了个裸 HTTP 400（opencode 把 4xx 判为不可重试）时用这条。
# 这类是偶发/上游故障，跟会话内容无关；同一个会话再催一次往往就能过。
RESUME_PROMPT_ERROR = """上一轮的模型请求被上游以 HTTP 400（InvalidRequest）拒绝，会话被迫中断，
PAD 要求的 ${PAD_WORK_DIR}/result.json 也没能写出来。这是上游的偶发故障，不是你的错。

不要重头再来。先只读检查 ${PAD_WORK_DIR} 里已经产出的东西、上次做到哪一步、有没有半途中断的产物，
然后从断点继续把任务做完 —— 长任务仍要在**本轮内**用 sleep/tail 轮询到真正结束，
最后按契约写 ${PAD_WORK_DIR}/result.json。无法完成时仍须提交报告，并以非零退出码说明失败。"""



def augment_path():
    """保活环境（systemd / cron / launchd）给的 PATH 很窄，常找不到 CLI。

    特别地：nvm 装在旧版本下的 CLI（如 codex 在 v20 而默认 v24）只有把
    所有版本目录都收进来才找得到，不能只取"当前默认版本"。
    """
    home = Path.home()
    extra = [home / ".local/bin", home / ".npm-global/bin", home / ".opencode/bin",
             home / ".bun/bin", home / ".cargo/bin",
             Path("/opt/homebrew/bin"), Path("/usr/local/bin"), Path("/snap/bin")]
    extra += sorted(home.glob(".nvm/versions/node/*/bin"), reverse=True)

    parts = [p for p in os.environ.get("PATH", "").split(os.pathsep) if p]
    for e in extra:
        if str(e) not in parts and e.is_dir():
            parts.append(str(e))
    os.environ["PATH"] = os.pathsep.join(parts)


augment_path()


_OC_MAJOR = None


def opencode_major() -> int:
    """探本机 opencode 的大版本（2=新版，1=老版）。取不到就按 1 处理（保守）。

    v1 与 v2 的 `run` 参数不同（v2 删了 --dir、多了 --standalone），而 pad 的骨架
    是**多节点共享**的：远端还有跑 v1.18 的机器，所以这里按版本分派，别写死一种。
    """
    global _OC_MAJOR
    if _OC_MAJOR is not None:
        return _OC_MAJOR
    try:
        out = subprocess.run(["opencode", "--version"], capture_output=True,
                             text=True, timeout=30).stdout
        m = re.search(r"(\d+)\.", out or "")
        _OC_MAJOR = int(m.group(1)) if m else 1
    except Exception:
        _OC_MAJOR = 1
    return _OC_MAJOR


def session_title(task_id: str) -> str:
    """本次派发给会话打的固定标题，用于收尾后反查同一个会话。"""
    return f"pad:{task_id}"


def build_cmd(harness: str, prompt: str, ws: str, task_id: str = "") -> list:
    """各家的无人值守调用方式。这里就是全部的适配代码。"""
    if harness == "opencode":
        # 工作目录统一靠子进程 cwd（见 main() 的 subprocess.run(..., cwd=ws)）。
        if opencode_major() >= 2:
            # v2 注意 1：`--dir` 已删除，加了会 `Unrecognized flag: --dir`。
            # v2 注意 2：默认走全用户共享的后台服务（opencode serve --service），共享
            # 服务的事件流一旦被挤断，CLI 会 `Transport: InvalidHTTPResponse ...
            # /api/event` 直接 exit 1（而 session 还在服务端跑）。无头派发用
            # --standalone 起私有 server，生命周期跟着这次任务走。
            # v2 注意 3：`--title` 给会话打上固定标题「pad:<task_id>」，这样 rc==0 但
            # 没写 result.json 时能按标题把同一个会话找回来续跑（见 resume_once）。
            args = ["opencode", "run", "--standalone", "--auto"]
            if task_id:
                args += ["--title", session_title(task_id)]
            return args + [prompt]
        # v1：保持原有形态（实测可用）
        return ["opencode", "run", "--auto", "--dir", ws, prompt]
    if harness == "claude":
        return ["claude", "-p", "--dangerously-skip-permissions", prompt]
    if harness == "codex":
        return ["codex", "exec", "-C", ws, "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox", prompt]
    sys.exit(f"未知执行体: {harness}（可选 opencode / claude / codex）")


def _oc_dir_env(wd: Path) -> dict:
    """opencode 用 `PWD` 环境变量（不是真实 cwd）判定会话所属目录。

    无头派发时 PWD 是从保活环境里继承的（ld_agent 的 shell 把它设成 work_dir），
    所以反查/续跑必须显式把 PWD 指到 wd，才能命中同一个会话。
    """
    env = dict(os.environ)
    env["PWD"] = str(wd)
    return env


def find_session_id(task_id: str, wd: Path) -> str:
    """按 build_cmd 打的标题「pad:<task_id>」反查本次会话 id；找不到返回空串。"""
    if not task_id:
        return ""
    want = session_title(task_id)
    try:
        out = subprocess.run(
            ["opencode", "session", "list", "--standalone", "--format", "json", "-n", "20"],
            capture_output=True, text=True, cwd=str(wd), env=_oc_dir_env(wd),
            timeout=120)
    except Exception as exc:
        print(f"[harness] session 反查失败：{exc}", file=sys.stderr)
        return ""
    try:
        items = json.loads((out.stdout or "").strip() or "[]")
    except Exception:
        return ""
    for it in items if isinstance(items, list) else []:
        if isinstance(it, dict) and it.get("title") == want and it.get("id"):
            return str(it["id"])
    return ""


# codex 的会话 id 只能从 run.log 头部抓：`codex exec` 每次都会先打一行
# `session id: <uuid>`（没有 opencode 那样的 --title 可用来反查）。
_CODEX_SID_RE = re.compile(r"^\s*session id:\s*([0-9a-fA-F-]{36})\s*$", re.M)


def codex_session_id(log_path: Path) -> str:
    """从 run.log 抓 codex 本次会话 id；找不到返回空串。

    只取**第一个**匹配：续跑会把新的 `session id:` 追加到日志末尾，
    而我们要续的始终是最初那个会话。
    """
    try:
        txt = log_path.read_text(errors="ignore")
    except Exception:
        return ""
    m = _CODEX_SID_RE.search(txt)
    return m.group(1) if m else ""


def resume_once(harness: str, task_id: str, wd: Path, ws: str, log_path: Path,
                sid: str = "", prompt_text: str = "") -> int:
    """没写 result.json 就收尾时，按同一会话续跑一次。

    返回续跑进程的 rc；没执行（不适用/找不到会话）返回 None。
    """
    prompt = (prompt_text or RESUME_PROMPT).replace("${PAD_WORK_DIR}", str(wd))
    if harness == "opencode":
        if opencode_major() < 2:
            return None
        sid = sid or find_session_id(task_id, wd)
        if not sid:
            print("[harness] 未找到本次会话，跳过自动续跑", file=sys.stderr)
            return None
        args = ["opencode", "run", "--standalone", "--auto", "--session", sid, prompt]
        run_env = _oc_dir_env(wd)
    elif harness == "codex":
        sid = sid or codex_session_id(log_path)
        if not sid:
            print("[harness] run.log 里没抓到 codex 会话 id，跳过自动续跑", file=sys.stderr)
            return None
        # 注意：`codex exec resume` 没有 `-C`（工作目录靠子进程 cwd，见 main()）。
        args = ["codex", "exec", "resume", sid, "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox", prompt]
        run_env = None            # 不覆盖 PWD：codex 不看 PWD
    else:
        return None
    print(f"[harness] 按会话 {sid} 自动续跑一次…", file=sys.stderr)
    with log_path.open("a") as log:
        log.write(f"\n\n# [harness] 无 result.json —— 会话 {sid} 自动续跑一次\n\n")
        log.flush()
        try:
            proc = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT,
                                  cwd=ws, env=run_env)
        except FileNotFoundError:
            return None
    return proc.returncode



def _opencode_log_path() -> Path:
    """opencode 服务端日志文件路径（--standalone 的私有 server 也写这里）。"""
    try:
        out = subprocess.run(["opencode", "debug", "paths", "log"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        if out:
            return Path(out) / "opencode.log"
    except Exception:
        pass
    return Path.home() / ".local/share/opencode/log/opencode.log"


def detect_provider_400(wd: Path, sid: str) -> bool:
    """判断这次 rc!=0 是不是上游回了个裸 HTTP 400（opencode 判为不可重试的 InvalidRequest）。

    先看 run.log（CLI 偶尔会原样带出来）；再在 opencode 服务端日志里**按 sessionID 精确匹配**
    'Failed to drain Session ... Provider request failed with HTTP 400'，避免撞到别的会话的旧记录。
    """
    try:
        txt = (wd / "run.log").read_text(errors="ignore")
    except Exception:
        txt = ""
    if re.search(r"provider\.invalid-request|Provider request failed", txt, re.I):
        return True
    lp = _opencode_log_path()
    if not sid or not lp.exists():
        return False
    try:
        size = lp.stat().st_size
        with lp.open("rb") as f:
            f.seek(max(0, size - 4 * 1024 * 1024))
            tail = f.read().decode("utf-8", "ignore")
    except Exception:
        return False
    for line in tail.splitlines():
        if sid in line and "HTTP 400" in line and (
                "Failed to drain Session" in line or "Provider request failed" in line):
            return True
    return False


def harvest(res: Path, seconds: int) -> bool:
    """CLI 提前断流退出时，服务端 session 往往还在跑并稍后补落 result.json。

    v2 的后台服务/session 生命周期独立于 CLI 进程：CLI 报 exit 1 不代表活没干成。
    这里给一段宽限期轮询 result.json，避免把「已经干完的活」报成失败。
    宽限期用 PAD_HARVEST_SEC 调（默认 180s，0=不等）。
    """
    if seconds <= 0 or res.exists():
        return res.exists()
    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(5)
        if res.exists():
            return True
    return res.exists()


def main() -> int:
    harness = (sys.argv[1] if len(sys.argv) > 1 else "opencode").lower()
    env = dict(os.environ)
    wd = Path(env["PAD_WORK_DIR"])
    wd.mkdir(parents=True, exist_ok=True)
    try:
        harvest_sec = int(env.get("PAD_HARVEST_SEC", "180"))
    except ValueError:
        harvest_sec = 180

    prompt = re.sub(r"\$\{(\w+)\}", lambda m: env.get(m.group(1), ""), TMPL.read_text())
    (wd / "prompt.md").write_text(prompt)

    ws = env.get("PAD_WORKSPACE", str(Path.home() / "pad-workspace"))
    task_id = env.get("PAD_TASK_ID", "")
    args = build_cmd(harness, prompt, ws, task_id)

    t0 = time.time()
    with (wd / "run.log").open("w") as log:
        log.write(f"# harness={harness}\n# PATH={os.environ['PATH']}\n"
                  f"# cmd={' '.join(args[:-1])} <prompt>\n\n")
        log.flush()
        try:
            proc = subprocess.run(args, stdout=log, stderr=subprocess.STDOUT, cwd=ws)
        except FileNotFoundError:
            exe = args[0]
            where = subprocess.run(["/usr/bin/which", exe], capture_output=True, text=True)
            found = where.stdout.strip() or "（which 也找不到）"
            msg = (f"执行体 `{exe}` 不在 PATH 里。which 结果：{found}。\n"
                   f"修法：把 {exe} 所在目录加进本节点 service 的 PATH，"
                   f"或在配置里把 cmd 写成 {exe} 的绝对路径。")
            log.write("\n" + msg + "\n")
            print(msg.splitlines()[0][:400])
            return 1
    dur = int(time.time() - t0)

    print(f"[harness:{harness}] 退出 {proc.returncode}，用时 {dur}s，日志 {wd}/run.log",
          file=sys.stderr)

    res = wd / "result.json"
    rc = proc.returncode
    if not res.exists() and rc != 0 and harvest_sec > 0:
        print(f"executor 非零退出且暂无 result.json，等 {harvest_sec}s 看 session 是否收尾…",
              file=sys.stderr)
        with (wd / "run.log").open("a") as log:
            log.write(f"\n# [harness] executor exit {rc}，"
                      f"进入 {harvest_sec}s harvest 等待（session 可能在服务端继续跑）\n")
        harvest(res, harvest_sec)

    # 没写 result.json 就收尾 / 上游裸 400：按同一个会话续跑一次（PAD_RESUME_ONCE=0 关闭）。
    #   - rc==0：执行体「提前收尾」（长任务放会话后台就结束了本轮，进程退出把后台一并带走）。
    #   - rc!=0 且是 provider HTTP 400：opencode 把 4xx 判为不可重试，偶发上游故障会直接带走整个会话。
    # opencode v2 / codex 支持续跑（codex 的会话 id 从 run.log 抓，见 codex_session_id）；
    # claude 没有可续的会话概念，自动跳过。rc!=0 的裸 400 识别是 opencode 专属，
    # 其他执行体只做 rc==0 那一次（不确定的错误不硬续，避免把真失败拖成假成功）。
    resume_enabled = os.environ.get("PAD_RESUME_ONCE", "1") not in ("0", "")
    can_resume = (harness == "opencode" and opencode_major() >= 2) or harness == "codex"
    rc2 = None
    if not res.exists() and resume_enabled and can_resume:
        if rc == 0:
            rc2 = resume_once(harness, task_id, wd, ws, wd / "run.log")
        elif harness == "opencode":
            sid = find_session_id(task_id, wd)
            if sid and detect_provider_400(wd, sid):
                print(f"[harness] rc={rc} 疑似上游 HTTP 400（不可重试），按会话 {sid} 续跑一次…",
                      file=sys.stderr)
                rc2 = resume_once(harness, task_id, wd, ws, wd / "run.log",
                                  sid=sid, prompt_text=RESUME_PROMPT_ERROR)
            elif sid:
                print(f"[harness] rc={rc} 非零退出，但未识别为可续跑的上游错误，跳过续跑",
                      file=sys.stderr)
    if rc2 is not None:
        rc = rc2
        if not res.exists() and rc != 0 and harvest_sec > 0:
            harvest(res, harvest_sec)

    if res.exists():
        try:
            print(str(json.loads(res.read_text()).get("summary", "完成"))[:400])
            return rc
        except Exception as exc:
            print(f"result.json 解析失败: {exc}")
            return 1

    if rc != 0:
        print(f"{harness} 非零退出（{rc}），且没有 result.json")
    else:
        tail = [l.strip() for l in (wd / "run.log").read_text(errors="ignore").splitlines() if l.strip()]
        print((tail[-1] if tail else "无输出")[:400])
    return 1  # 没有结构化结果就不能把任务报告成 done


if __name__ == "__main__":
    sys.exit(main())
