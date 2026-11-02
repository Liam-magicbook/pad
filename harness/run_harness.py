#!/usr/bin/env python3
"""Translate one PAD task into a headless call of some local agent CLI, and collapse the result into one line.

Usage:
    run_harness.py [opencode|claude|codex]        # opencode by default

PAD itself does not care which executor you use — it only sends "what to do". The adaptation lives here,
decided by each node. Switching executors means switching this one argument.

To keep behavior uniform, all three executors close out under the same contract:
  the prompt is rendered from harness/prompt.md.tmpl
  whether done or unable to finish, write $PAD_WORK_DIR/result.json; the exit code distinguishes done / failed
  full output stays in $PAD_WORK_DIR/run.log and is never uploaded
  when rc==0 but result.json is missing (the executor "wrapped up early"), opencode v2 / codex are nudged
  once more in the same session; claude has no resumable session, so it is skipped (PAD_RESUME_ONCE=0 disables it).
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

# When rc==0 but result.json is missing, the executor most likely wrapped up early (parked a long task in the
# session background and ended the turn, taking it down on exit). Nudge the same session once more so it can finish.
RESUME_PROMPT = """上一轮你把对话收尾了，但 PAD 要求的 ${PAD_WORK_DIR}/result.json 并没有写出来，
说明任务很可能没真正完成（常见原因：把长任务放到会话后台就结束了本轮，进程一退，后台连带被杀）。

不要重头再来。先只读地检查 ${PAD_WORK_DIR} 里已经产出的东西、以及有没有半途中断的产物，
判断还差哪些必需步骤；然后在**本轮内**把它们做完 —— 长任务要在这一轮里用 sleep/tail 轮询到
真的结束（不要再次抛下就收尾）。最后按契约写 ${PAD_WORK_DIR}/result.json。
如果穷尽合理路径仍无法完成，就写 ${PAD_WORK_DIR}/result.json，如实说明尝试、阻碍和未完成项。"""

# Used when rc!=0 and the upstream provider returned a bare HTTP 400 (opencode treats 4xx as non-retryable).
# These are transient/upstream failures unrelated to the session content; nudging the same session once usually gets through.
RESUME_PROMPT_ERROR = """上一轮的模型请求被上游以 HTTP 400（InvalidRequest）拒绝，会话被迫中断，
PAD 要求的 ${PAD_WORK_DIR}/result.json 也没能写出来。这是上游的偶发故障，不是你的错。

不要重头再来。先只读检查 ${PAD_WORK_DIR} 里已经产出的东西、上次做到哪一步、有没有半途中断的产物，
然后从断点继续把任务做完 —— 长任务仍要在**本轮内**用 sleep/tail 轮询到真正结束，
最后按契约写 ${PAD_WORK_DIR}/result.json。无法完成时仍须提交报告，并以非零退出码说明失败。"""



def augment_path():
    """Keep-alive environments (systemd / cron / launchd) hand out a very narrow PATH, so CLIs are often not found.

    In particular: a CLI installed under an older nvm version (codex on v20 while the default is v24) is only
    found if every version dir is collected — taking only the "current default version" is not enough.
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
    """Detect the local opencode major version (2=new, 1=old). Fall back to 1 when detection fails (conservative).

    v1 and v2 take different `run` arguments (v2 dropped --dir and added --standalone), and pad's skeleton
    is **shared across nodes**: remotes still run v1.18, so dispatch by version here instead of hardcoding one.
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
    """Fixed title stamped on the session for this dispatch, used to find the same session again after close-out."""
    return f"pad:{task_id}"


def build_cmd(harness: str, prompt: str, ws: str, task_id: str = "") -> list:
    """Each vendor's unattended invocation. This is the entire adapter."""
    if harness == "opencode":
        # Working dir comes from the child process cwd (see subprocess.run(..., cwd=ws) in main()).
        if opencode_major() >= 2:
            # v2 note 1: `--dir` is gone; passing it yields `Unrecognized flag: --dir`.
            # v2 note 2: by default it uses a user-wide shared background service (opencode serve --service);
            # once that shared service's event stream drops, the CLI exits 1 with `Transport: InvalidHTTPResponse ...
            # /api/event` while the session keeps running server-side. Headless dispatch uses --standalone to
            # start a private server whose lifetime follows this task.
            # v2 note 3: `--title` stamps the fixed title "pad:<task_id>" so that, when rc==0 but result.json
            # is missing, the same session can be found by title and resumed (see resume_once).
            args = ["opencode", "run", "--standalone", "--auto"]
            if task_id:
                args += ["--title", session_title(task_id)]
            return args + [prompt]
        # v1: keep the original shape (verified working)
        return ["opencode", "run", "--auto", "--dir", ws, prompt]
    if harness == "claude":
        return ["claude", "-p", "--dangerously-skip-permissions", prompt]
    if harness == "codex":
        return ["codex", "exec", "-C", ws, "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox", prompt]
    sys.exit(f"未知执行体: {harness}（可选 opencode / claude / codex）")


def _oc_dir_env(wd: Path) -> dict:
    """opencode decides which directory a session belongs to from the `PWD` env var, not the real cwd.

    In headless dispatch PWD is inherited from the keep-alive environment (ld_agent's shell sets it to work_dir),
    so lookups and resumes must explicitly point PWD at wd to hit the same session.
    """
    env = dict(os.environ)
    env["PWD"] = str(wd)
    return env


def find_session_id(task_id: str, wd: Path) -> str:
    """Find this dispatch's session id by the "pad:<task_id>" title set in build_cmd; return an empty string if not found."""
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


# codex's session id can only be scraped from the head of run.log: every `codex exec` first prints a line
# `session id: <uuid>` (there is no --title to look up, unlike opencode).
_CODEX_SID_RE = re.compile(r"^\s*session id:\s*([0-9a-fA-F-]{36})\s*$", re.M)


def codex_session_id(log_path: Path) -> str:
    """Scrape codex's session id for this dispatch from run.log; return an empty string if not found.

    Take only the **first** match: resumes append new `session id:` lines to the end of the log,
    and we always want to resume the original session.
    """
    try:
        txt = log_path.read_text(errors="ignore")
    except Exception:
        return ""
    m = _CODEX_SID_RE.search(txt)
    return m.group(1) if m else ""


def resume_once(harness: str, task_id: str, wd: Path, ws: str, log_path: Path,
                sid: str = "", prompt_text: str = "") -> int:
    """When the run closes out without result.json, resume once in the same session.

    Returns the resume process rc; returns None when nothing ran (not applicable / session not found).
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
        # Note: `codex exec resume` has no `-C` (working dir comes from the child cwd, see main()).
        args = ["codex", "exec", "resume", sid, "--skip-git-repo-check",
                "--dangerously-bypass-approvals-and-sandbox", prompt]
        run_env = None            # do not override PWD: codex ignores it
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
    """Path to the opencode server log file (the --standalone private server writes here too)."""
    try:
        out = subprocess.run(["opencode", "debug", "paths", "log"],
                             capture_output=True, text=True, timeout=30).stdout.strip()
        if out:
            return Path(out) / "opencode.log"
    except Exception:
        pass
    return Path.home() / ".local/share/opencode/log/opencode.log"


def detect_provider_400(wd: Path, sid: str) -> bool:
    """Decide whether this rc!=0 was a bare HTTP 400 from upstream (an InvalidRequest opencode treats as non-retryable).

    Check run.log first (the CLI sometimes surfaces it verbatim); then, in the opencode server log, **exactly match
    by sessionID** against 'Failed to drain Session ... Provider request failed with HTTP 400' to avoid stale records.
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
    """When the CLI dies early, the server-side session is often still running and drops result.json later.

    v2's background service/session lifetime is independent of the CLI process: a CLI exit 1 does not mean the job failed.
    Poll result.json for a grace period here so finished work is not reported as failure.
    Tune the grace period with PAD_HARVEST_SEC (default 180s, 0=do not wait).
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

    # Closed out without result.json / upstream bare 400: resume once in the same session (PAD_RESUME_ONCE=0 disables).
    #   - rc==0: the executor wrapped up early (parked a long task in the session background and ended the turn,
    #     and exiting took it down). rc!=0 with a provider HTTP 400: opencode treats 4xx as non-retryable, so a
    #     transient upstream fault takes the whole session with it. opencode v2 / codex support resuming (codex's
    #     session id is scraped from run.log, see codex_session_id); claude has no resumable session concept and is
#     skipped. Bare-400 detection is opencode-only; other executors get just the rc==0 nudge (no forced resume on an
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
    return 1  # no structured result means the task cannot be reported as done


if __name__ == "__main__":
    sys.exit(main())
