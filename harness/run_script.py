#!/usr/bin/env python3
"""跑一条确定性命令，把结果收敛成 PAD 的契约（和 run_harness.py 同一套）。

用法（由 ld_config 的 cmd 调起）：
    run_script.py "bash my-daily.sh; tail -20 /tmp/my-daily-$(date +%F).log"

为什么末尾要接 tail：脚本大多自己 `exec >> 日志`，
stdout 是空的，不 tail 一下 PAD 就只能看到"退出码 0"，等于没汇报。

契约：
  退出 0            → done，summary = 输出的最后一行
  非 0              → failed
  非零退出 → 写入 failed 报告，交由 PAD 保留结果
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
    """脚本也会写验收汇报（不是只有 LLM 执行体才写）。

    脚本能给出来的就是：退出码、用时、跑了什么、日志尾巴。给不出的（比如"文章主题是什么"）
    不编，放在 run.log 里让上层按需取——run.log 会被自动存一份到服务器。
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
    """脚本判定不了验收真伪，但必须逐条 answering：说白了就是"这条我没法自证，证据在哪"。"""
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
