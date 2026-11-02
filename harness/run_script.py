#!/usr/bin/env python3
"""Run one deterministic command and collapse the result into PAD's contract (same shape as run_harness.py).

Usage (invoked from ld_config's cmd):
    run_script.py "bash my-daily.sh; tail -20 /tmp/my-daily-$(date +%F).log"

Why tail at the end: most scripts `exec >> log` themselves, so
stdout is empty; without a tail, PAD only sees "exit code 0", which is no report at all.

Contract:
  exit 0            -> done, summary = the last line of output
  non-zero          -> failed
  non-zero exit     -> write a failed report and let PAD keep the result
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent


def compose_report(command: str, code: int, dur: int, tail: list) -> str:
    """Scripts write acceptance reports too (it is not just LLM executors that do).

    All a script can offer is exit code, duration, what ran, and the log tail. Anything else (say "what is this
    article about") is not invented, just left in run.log for the platform to fetch — run.log is auto-stored server-side.
    """
    lines = [
        f"【结论】{'命令执行成功' if code == 0 else f'命令失败（退出码 {code}）'}，用时 {dur}s。",
        "",
        "【执行】" + command,
        "",
        "【日志尾部】",
    ]
    lines += [f"- {l}" for l in tail[-15:]] or ["- （日志为空）"]
    lines += ["", "【说明】这是脚本化流水线，不是逐步汇报。完整日志 run.log 已存一份到服务器，"
                  "要看某一段用 get_task(artifact=\"run.log\", offset=...) 取。"]
    return "\n".join(lines)


def acceptance_results() -> list:
    """A script cannot judge acceptance, but must answer each item: "I cannot prove this — evidence is here"."""
    raw = os.environ.get("PAD_ACCEPTANCE", "")
    items = [l.strip()[2:].strip() for l in raw.splitlines() if l.strip().startswith("- ")]
    return [{"item": a, "passed": None,
             "evidence": "脚本化流水线不自证；证据：run.log（已存服务器）+ report【日志尾部】"}
            for a in items]


def augment_path():
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


def main() -> int:
    if len(sys.argv) < 2:
        print("用法: run_script.py <shell command>")
        return 1
    command = sys.argv[1]

    env = dict(os.environ)
    wd = Path(env["PAD_WORK_DIR"])
    wd.mkdir(parents=True, exist_ok=True)
    ws = env.get("PAD_WORKSPACE", str(Path.home() / "pad-workspace"))

    t0 = time.time()
    with (wd / "run.log").open("w") as log:
        log.write(f"# script: {command}\ncwd: {ws}\n\n")
        log.flush()
        proc = subprocess.run(command, shell=True, stdout=log, stderr=subprocess.STDOUT,
                              cwd=ws, env=env)
    dur = int(time.time() - t0)

    print(f"[script] 退出 {proc.returncode}，用时 {dur}s，日志 {wd}/run.log", file=sys.stderr)

    lines = [l.strip() for l in
             (wd / "run.log").read_text(errors="ignore").splitlines() if l.strip()]
    tail = [l for l in lines if not l.startswith("#") and not l.startswith("cwd:")]
    last = (tail[-1] if tail else f"命令退出 {proc.returncode}")[:400]

    (wd / "result.json").write_text(json.dumps({
        "status": "done" if proc.returncode == 0 else "failed",
        "summary": last,
        "report": compose_report(command, proc.returncode, dur, tail),
        "acceptance_results": acceptance_results(),
        "artifacts": [{"name": "run.log", "path": str(wd / "run.log"), "upload": True}],
    }, ensure_ascii=False, indent=2))

    if proc.returncode == 0:
        print(last)
        return 0

    print(last)
    return 1


if __name__ == "__main__":
    sys.exit(main())
