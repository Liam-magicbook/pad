"""Product LD agent — one instance runs on every machine.

It does four things:
  1. Register this node's capabilities with PAD
  2. Poll for work (by capability, not by machine)
  3. Run the job, package the results into a report and send it back
  4. The executor explores and finishes the task itself; the final done / failed report is written back to PAD

Usage:
    PAD_KEY=xxx python3 ld_agent.py ld_config.yaml
"""
import hashlib
import json
import re
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

import bundle

CST = timezone(timedelta(hours=8))


def ts() -> str:
    return datetime.now(CST).strftime("%H:%M:%S")


class LD:
    # Cap on a single report payload (the server comfortably takes >=12KB; keep headroom — degrade, never drop)
    MAX_PAYLOAD = 16000
    # Per-file cap for auto-uploaded artifacts; anything larger stays local with metadata only
    ARTIFACT_MAX_BYTES = 5 * 1024 * 1024
    # After a session timeout, enter lease watch: keep waiting on / harvesting descendants that are still running;
    # if still alive at expiry, report unresolved (task stays non-terminal, swept later) — never fail a task on a timeout.
    DEFAULT_LEASE_MIN = 60
    LEASE_POLL_SEC = 10
    # How often to poll PAD task state while the executor runs: on cancelled, kill the process group (cancel propagation)
    CANCEL_POLL_SEC = 15
    KILL_GRACE_SEC = 10

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.node_id = cfg["node_id"]
        self.caps = list(cfg["capabilities"].keys())
        self.base = cfg["pad_url"].rstrip("/")
        key = os.environ.get("PAD_KEY") or cfg.get("pad_key")
        if not key:
            keyfile = Path.home() / ".pad_key"
            key = keyfile.read_text().strip() if keyfile.exists() else None
        if not key:
            sys.exit("缺少 PAD_KEY（env / config / ~/.pad_key 三选一）")
        self.h = {"X-PAD-Key": key, "Content-Type": "application/json; charset=utf-8"}
        self.base = f"{self.base}"
        self.work_root = Path(cfg.get("work_dir", "/tmp/pad-work"))
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.outbox = self.work_root / "outbox.jsonl"   # undeliverable reports land here; the main loop retries
        self._out_lock = threading.Lock()
        self._active = {}                               # currently running: task_id -> {capability, t0}
        self._active_lock = threading.Lock()

    def _post(self, path, payload):
        try:
            r = requests.post(f"{self.base}/api/{path}", json=payload, headers=self.h, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            # self.base contains the PAD key in its path; never persist the raw URL in logs.
            raise RuntimeError(f"PAD API {path} request failed ({type(exc).__name__})") from None

    def _get(self, path, **params):
        try:
            r = requests.get(f"{self.base}/api/{path}", params=params, headers=self.h, timeout=30)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            raise RuntimeError(f"PAD API {path} request failed ({type(exc).__name__})") from None

    # ---- Report delivery (never lose one silently) ----
    #
    # 2026-09-17 incident: report() POSTed directly, and when the gateway returned 502 the exception was
    # swallowed by an outer except — the work got done, artifacts sat locally, and the platform never knew.
    # Worse: all 5 of 5 attempts returned 200 while the server stored an empty shell payload. An ack is not proof.
    # So now: POST -> **read back to confirm** (is this client_id findable in task_view) ->
    # unconfirmed -> resend -> still failing -> drop into outbox, retried every main-loop tick until it is really online.
    # Every report carries a client_id and the server dedupes on it, so resends never produce duplicate reports.

    def _attempt(self, path: str, body: dict, quiet: bool = False) -> tuple[str, dict | None]:
        """One delivery attempt. ok=confirmed stored; reject=server refused outright (resend is useless); retry=try again."""
        try:
            r = requests.post(f"{self.base}/api/{path}", json=body, headers=self.h, timeout=20)
        except requests.RequestException as exc:
            if not quiet:
                print(f"[{ts()}] {path} 投递失败 ({type(exc).__name__})")
            return "retry", None
        if r.status_code >= 500:
            if not quiet:
                print(f"[{ts()}] {path} 服务端 {r.status_code}")
            return "retry", None
        if r.status_code >= 400:
            print(f"[{ts()}] {path} 被拒 {r.status_code}: {r.text[:200]}")
            return "reject", None
        if not self._confirmed(path, body):
            if not quiet:
                print(f"[{ts()}] {path} 返回 200 但线上读不到 {body.get('client_id', '')}")
            return "retry", None
        return "ok", r.json()

    def _confirmed(self, path: str, body: dict) -> bool:
        """Read back: did this report really reach the online state? Missing means it never arrived — no self-deception."""
        try:
            t = self._get(f"task/{body['task_id']}")
        except Exception as exc:
            print(f"[{ts()}] 读回确认失败 ({type(exc).__name__})")
            return False
        items = t.get("reports", []) or []
        cid = body.get("client_id")
        if cid and any(x.get("client_id") == cid for x in items):
            return True
        if any(x.get("client_id") for x in items):
            return False                      # server does store client_id, this one absent -> it never arrived
        return any(x.get("level") == body.get("level") and x.get("summary") == body.get("summary")
                   for x in items)

    def _deliver(self, path: str, body: dict, attempts: int = 3) -> dict | None:
        for i in range(attempts):
            verdict, out = self._attempt(path, body)
            if verdict == "ok":
                return out
            if verdict == "reject":
                return None
            if i < attempts - 1:
                time.sleep(1 + 2 * i)
        self._queue(path, body)
        return None

    def _queue(self, path: str, body: dict) -> None:
        with self._out_lock:
            with self.outbox.open("a") as f:
                f.write(json.dumps({"path": path, "body": body}, ensure_ascii=False) + "\n")
        print(f"[{ts()}] 已入 outbox 待补投：{path} {body.get('task_id', '')}")

    def flush_outbox(self) -> None:
        with self._out_lock:
            if not self.outbox.exists():
                return
            lines = [l for l in self.outbox.read_text().splitlines() if l.strip()]
            self.outbox.unlink(missing_ok=True)
            if not lines:
                return
            left = []
            for line in lines:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                verdict, _ = self._attempt(e["path"], e["body"], quiet=True)
                if verdict == "retry":
                    left.append(line)
                elif verdict == "ok":
                    print(f"[{ts()}] outbox 补投并已确认：{e['path']} {e['body'].get('task_id', '')}")
            if left:
                self.outbox.write_text("\n".join(left) + "\n")
            if len(left) != getattr(self, "_out_pending", -1):   # only speak up when the number changes
                print(f"[{ts()}] outbox 待补投 {len(left)}/{len(lines)} 条")
                self._out_pending = len(left)

    # ---- Basic actions ----

    def register(self):
        detail = {k: v.get("desc", "") for k, v in self.cfg["capabilities"].items()}
        mine = bundle.bundle_hash(Path(__file__).resolve().parent)
        r = self._post("register", {"node_id": self.node_id, "role": self.cfg.get("role", ""),
                                    "description": self.cfg.get("description", ""),
                                    "capabilities": self.caps,
                                    "bundle_hash": mine,
                                    "meta": {"capabilities_detail": detail,
                                             **self.cfg.get("meta", {})}})
        srv = r.get("server_bundle_hash")
        if srv and srv != mine:
            print(f"\n{'!'*70}\n"
                  f"[{ts()}] 本机骨架已过期：本地 {mine} / 服务端 {srv}\n"
                  f"            你正在用旧的提示词和旧规矩干活，会重复踩已经修过的坑。\n"
                  f"            立刻重拉（不会动 ld_config.yaml）：\n"
                  f"              curl -fsS \"$BASE/agent.bundle.tgz\" | tar xz -C \"$PAD_HOME\"\n"
                  f"            改完重启本进程。\n{'!'*70}\n")
            self.bundle_stale = True
        else:
            self.bundle_stale = False
        print(f"[{ts()}] registered {self.node_id} caps={self.caps}")

    def beat(self, summary):
        self._post("heartbeat", {"node_id": self.node_id, "summary": summary})

    def claim(self, capabilities=None):
        """Claim a job. capabilities carries only the caps with a free slot, so nothing is claimed that cannot run."""
        caps = list(self.caps if capabilities is None else capabilities)
        if not caps:
            return None
        return self._post("claim", {"node_id": self.node_id, "capabilities": caps})["task"]

    INPUT_FILE_MAX_BYTES = 10 * 1024 * 1024
    INPUT_FILE_MAX_COUNT = 8

    def _receive_input_files(self, task, tdir: Path) -> list[dict]:
        """Download the input files shipped with the task into the local task dir, verifying byte count and SHA-256.

        The server puts "images" and "plain files" in one manifest (input_files, formerly input_images);
        here everything is bytes: images/PDFs/CSVs/zips are just files, and making sense of them is the executor's call.
        """
        files = task.get("input_files")
        if files is None:
            files = task.get("input_images") or []
        if not files:
            return []
        if not isinstance(files, list) or len(files) > self.INPUT_FILE_MAX_COUNT:
            raise ValueError("任务输入文件清单无效")
        target = tdir / "inputs"
        target.mkdir(parents=True, exist_ok=True)
        received = []
        for item in files:
            name = str(item.get("name") or "").replace("\\", "/").split("/")[-1]
            if not name or name in (".", ".."):
                raise ValueError("输入文件名无效")
            expected_bytes = int(item.get("bytes", -1))
            if expected_bytes < 0 or expected_bytes > self.INPUT_FILE_MAX_BYTES:
                raise ValueError("输入文件大小超出节点接收上限")
            try:
                r = requests.get(f"{self.base}/api/task/{task['task_id']}/input-file",
                                 params={"name": name}, headers=self.h,
                                 timeout=(15, 120))
                r.raise_for_status()
            except requests.RequestException:
                raise RuntimeError("PAD 输入文件下载请求失败") from None
            data = r.content
            if len(data) != expected_bytes:
                raise ValueError(f"输入文件长度校验失败: {name}")
            if r.headers.get("Content-Type", "").split(";", 1)[0] != item.get("mime_type"):
                raise ValueError(f"输入文件 MIME 校验失败: {name}")
            digest = hashlib.sha256(data).hexdigest()
            if digest != item.get("sha256") or r.headers.get("X-PAD-SHA256") != digest:
                raise ValueError(f"输入文件 SHA-256 校验失败: {name}")
            path = target / name
            path.write_bytes(data)
            received.append({"name": name, "mime_type": item.get("mime_type", ""),
                             "bytes": len(data), "sha256": digest, "path": str(path)})
        (target / "manifest.json").write_text(
            json.dumps(received, ensure_ascii=False, indent=2))
        return received

    # Legacy alias: external scripts and docs refer to this method name.
    _receive_input_images = _receive_input_files

    def report(self, task_id, level, summary, payload=None, client_id=None):
        payload = payload if payload is not None else {}
        if client_id is None:
            if level == "progress":
                client_id = uuid.uuid4().hex
            else:
                # Terminal reports get a deterministic id, so resend / outbox retry / post-restart resend all count once
                digest = hashlib.sha1(json.dumps([summary, payload], ensure_ascii=False,
                                                 sort_keys=True).encode()).hexdigest()[:8]
                client_id = f"{task_id}-{level}-{digest}"
        out = self._deliver("report", {"task_id": task_id, "node_id": self.node_id, "level": level,
                                       "summary": summary, "payload": payload,
                                       "client_id": client_id})
        if out is None:
            print(f"[{ts()}] report:{level} 未确认（已排入补投）{summary}")
        else:
            print(f"[{ts()}] report:{level} {summary}")

    # ---- Doing the work ----

    def run_task(self, task):
        cap = task["capability"]
        spec = self.cfg["capabilities"][cap]
        tdir = self.work_root / task["task_id"]
        tdir.mkdir(parents=True, exist_ok=True)
        self.report(task["task_id"], "progress", f"开始执行 {cap}")
        try:
            input_files = self._receive_input_files(task, tdir)
        except Exception as exc:
            self.report(task["task_id"], "failed", "任务输入文件未能交付给执行机", {
                "status": "failed",
                "report": "PAD 节点在启动执行体前未能完成输入文件下载或校验。",
                "input_file_error_type": type(exc).__name__,
            })
            return

        file_prompt = "\n".join(
            f"- {x['name']} ({x['mime_type']}, {x['bytes']} bytes): {x['path']}"
            for x in input_files) or "（无）"
        env = dict(os.environ)
        env.update({
            "PAD_TASK_ID": task["task_id"],
            "PAD_NODE": self.node_id,
            "PAD_CAPABILITY": cap,
            "PAD_GOAL": task["goal"],
            "PAD_ACCEPTANCE": "\n".join(f"- {a}" for a in task["acceptance"]) or "（未指定）",
            "PAD_CONSTRAINTS": "\n".join(f"- {c}" for c in task["constraints"]) or "（无）",
            "PAD_WORK_DIR": str(tdir),
            "PAD_WORKSPACE": self.cfg.get("workspace", str(Path.home() / "pad-workspace")),
            "PAD_INPUT_FILES": json.dumps(input_files, ensure_ascii=False),
            "PAD_INPUT_FILES_PROMPT": file_prompt,
            # Legacy variable names kept: older capability commands/scripts read them.
            "PAD_INPUT_IMAGES": json.dumps(input_files, ensure_ascii=False),
            "PAD_INPUT_IMAGES_PROMPT": file_prompt,
        })

        timeout_s = self._timeout_seconds(spec)
        info = self._exec(task["task_id"], spec["cmd"], tdir, env, timeout_s)
        info["timeout_sec"] = timeout_s
        info["started_at"] = datetime.now(CST).isoformat()
        (tdir / "run_state.json").write_text(json.dumps(
            {"rc": info.get("rc"), "timed_out": info.get("timed_out", False)},
            ensure_ascii=False))

        if info.get("cancelled"):                   # cancelled upstream: executor killed, no terminal state reported
            self._on_cancelled(task, tdir, info)
            return

        if info["timed_out"]:                       # session timeout != task failure; take the recovery path
            self._on_session_timeout(task, tdir, info, spec)
            return

        payload = self._collect_result(tdir, info["rc"])
        if self._result_level(payload, info["rc"]) == "done":
            summary = (payload.get("summary") or "").strip() or (self._tail(tdir) or f"{cap} 完成")
            self.report(task["task_id"], "done", summary[:400], payload)
        else:
            summary = (payload.get("summary") or self._tail(tdir) or "任务未完成").strip()
            self.report(task["task_id"], "failed", summary[:400], payload)

    # ---- Single executor/session lifecycle: decoupled from business Task state ----
    #
    # 2026-09-18 incident (wsl-kuo0fgi t-20260918-6a7222 / mac-1 t-20260917-a83e54):
    # the capability's 60min timeout killed only the outer sh; run_harness (grandchild) and claude/opencode
    # (great-grandchild) were adopted by init and ran for another 2h01m, fully producing result.json plus 16
    # artifacts — yet the old code went straight to report(failed) and returned from the TimeoutExpired branch,
    # **skipping _collect_result()**, so the work was done and PAD only saw a failure. Root cause: treating one
    # wrapper's hard timeout as a failed business Task. Rules of the fix:
    #   1) after any session timeout, harvest first; if a result exists, close out on that result;
    #   2) if a descendant is still alive, enter lease watch and report unresolved at expiry (task stays
    #      non-terminal, still collectable); never kill a live descendant that may still finish the task;
    #   3) only "executor has exited and there is no result at all" counts as a real failure.
    # Compatibility: capability/call convention unchanged; timeout_min is still the soft timeout; lease_min optional (60).

    def _timeout_seconds(self, spec) -> int:
        return int(spec.get("timeout_min", 4320)) * 60

    def _lease_seconds(self, spec) -> int:
        return int(spec.get("lease_min", self.DEFAULT_LEASE_MIN)) * 60

    @staticmethod
    def _group_alive(pgid) -> bool:
        if not pgid:
            return False
        try:
            os.killpg(int(pgid), 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _has_result(self, tdir: Path) -> bool:
        return (tdir / "result.json").is_file()

    def _task_status(self, task_id):
        """Look up the task's current state; return None on network errors (with no state, do not kill anything)."""
        try:
            return self._get(f"task/{task_id}/status").get("status")
        except Exception:
            return None

    def _kill_group(self, pgid, grace=None) -> None:
        """TERM then KILL, taking down the whole process group (the executor's descendants all live in it)."""
        grace = self.KILL_GRACE_SEC if grace is None else grace
        if not pgid:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(int(pgid), sig)
            except (ProcessLookupError, PermissionError):
                return
            deadline = time.time() + grace
            while time.time() < deadline:
                if not self._group_alive(pgid):
                    return
                time.sleep(0.5)

    def _tail(self, tdir: Path) -> str:
        f = tdir / "exec.log"
        if not f.is_file():
            return ""
        lines = [l.rstrip() for l in f.read_text(errors="ignore").splitlines() if l.strip()]
        return lines[-1] if lines else ""

    def _exec(self, task_id, cmd, tdir, env, timeout_s) -> dict:
        """Run one executor in its own process group.

        A timeout does **not** kill the process group: descendants may still be doing valid work (the incident proved it).
        But an upstream cancel must really stop it — poll state while running and kill the whole group on cancel.
        Output goes to exec.log (a file, not a pipe) so orphans cannot block on a full pipe.
        """
        logf = (tdir / "exec.log").open("w")
        t0 = time.time()
        proc = subprocess.Popen(cmd, shell=True, cwd=str(tdir), env=env,
                                stdout=logf, stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        pgid = proc.pid                     # start_new_session ⇒ the child is the session / process-group leader
        cancelled = threading.Event()
        stop_watch = threading.Event()

        def watch_cancel():
            while not stop_watch.wait(self.CANCEL_POLL_SEC):
                if proc.poll() is not None:
                    return
                if self._task_status(task_id) == "cancelled":
                    print(f"[{ts()}] {task_id} 已被上层取消 → 终止进程组 {pgid}")
                    cancelled.set()
                    self._kill_group(pgid)
                    return

        threading.Thread(target=watch_cancel, daemon=True).start()
        try:
            proc.wait(timeout=timeout_s)
            rc, timed_out = proc.returncode, False
        except subprocess.TimeoutExpired:
            rc, timed_out = None, True
        finally:
            stop_watch.set()
            logf.close()                    # close the parent's copy; the child still holds its own fd
        return {"rc": rc, "timed_out": timed_out, "pgid": pgid,
                "elapsed": int(time.time() - t0), "cancelled": cancelled.is_set()}

    def _harvest(self, tdir, info, code, state, reason) -> dict:
        payload = self._collect_result(tdir, code)
        payload.update(state=state, reason=reason,
                       descendant_alive=self._group_alive(info.get("pgid")),
                       pgid=info.get("pgid"), session_timeout_sec=info.get("timeout_sec"),
                       elapsed_sec=info.get("elapsed"))
        return payload

    def _finish_from_work(self, task, tdir, info, note) -> None:
        """If result.json exists, close out on its reported status (session timeout / exception / restart all land here)."""
        payload = self._harvest(tdir, info, info.get("rc"), "harvested", note)
        summary = (payload.get("summary") or "").strip() or f"{task['capability']} 已产出结果"
        self.report(task["task_id"], self._result_level(payload, info.get("rc")), summary[:400], payload)

    def _write_pending(self, tdir, task, info, timeout_s, lease_s) -> None:
        (tdir / "pending.json").write_text(json.dumps({
            "task_id": task["task_id"], "capability": task["capability"],
            "pgid": info.get("pgid"), "started_at": info.get("started_at"),
            "session_timeout_sec": timeout_s, "lease_sec": lease_s,
            "state": "unresolved", "at": datetime.now(CST).isoformat(),
        }, ensure_ascii=False))

    def _on_cancelled(self, task, tdir, info) -> None:
        """Cancelled upstream: executor is gone, report nothing terminal (control plane owns it); drop revival marker."""
        (tdir / "pending.json").unlink(missing_ok=True)
        (tdir / "cancelled.json").write_text(json.dumps({
            "task_id": task["task_id"], "capability": task["capability"],
            "pgid": info.get("pgid"), "at": datetime.now(CST).isoformat(),
        }, ensure_ascii=False))
        print(f"[{ts()}] {task['task_id']} 取消完成：执行体已终止，槽位释放")

    def _on_session_timeout(self, task, tdir, info, spec) -> None:
        cap = task["capability"]
        # 1) a result already exists: the work is actually done, close out on it and do not report failure
        if self._has_result(tdir):
            self._finish_from_work(task, tdir, info, "session_timeout_result_present")
            return
        # 2) descendant still alive: lease watch, keep harvesting throughout, never kill
        if self._group_alive(info.get("pgid")):
            lease_s = self._lease_seconds(spec)
            deadline = time.time() + lease_s
            while time.time() < deadline:
                if self._has_result(tdir):
                    self._finish_from_work(task, tdir, info, "harvested_during_lease")
                    return
                if not self._group_alive(info.get("pgid")):
                    break
                time.sleep(self.LEASE_POLL_SEC)
            if self._has_result(tdir):
                self._finish_from_work(task, tdir, info, "harvested_during_lease")
                return
            if self._group_alive(info.get("pgid")):
                # still running at expiry: report unresolved (task stays non-terminal) and leave a marker for sweep
                self._write_pending(tdir, task, info, info["timeout_sec"], lease_s)
                payload = {"state": "unresolved", "reason": "executor_session_timeout",
                           "descendant_alive": True, "pgid": info.get("pgid"),
                           "session_timeout_sec": info["timeout_sec"], "lease_sec": lease_s,
                           "elapsed_sec": info.get("elapsed"), "work_dir": str(tdir),
                           "summary": f"{cap} 会话超时，执行体仍在运行"}
                self.report(task["task_id"], "unresolved",
                            f"{cap} 会话超时，执行体仍在运行（已转后台回收）"[:400], payload)
                return
        # 3) descendant exited with no result: a real execution failure (still carry whatever artifacts exist)
        payload = self._collect_result(tdir, info.get("rc"))
        payload.update(state="failed", reason="executor_exited_no_result",
                       descendant_alive=False, pgid=info.get("pgid"), work_dir=str(tdir))
        self.report(task["task_id"], "failed",
                    (self._tail(tdir) or f"{cap} 会话超时且执行体已退出、无产物")[:400], payload)

    def sweep_pending(self) -> None:
        """Descendants left running by a session timeout: harvest any result; only "dead with no result" is a failure."""
        for marker in sorted(self.work_root.glob("*/pending.json")):
            tdir = marker.parent
            try:
                m = json.loads(marker.read_text())
            except Exception:
                marker.unlink(missing_ok=True)
                continue
            task = {"task_id": m["task_id"], "capability": m.get("capability", "")}
            info = {"pgid": m.get("pgid"), "rc": None, "elapsed": 0,
                    "timeout_sec": m.get("session_timeout_sec")}
            if self._task_status(m["task_id"]) == "cancelled":   # cancelled upstream, do not revive
                marker.unlink(missing_ok=True)
                continue
            if self._has_result(tdir):
                self._finish_from_work(task, tdir, info, "harvested_by_sweep")
                marker.unlink(missing_ok=True)
                continue
            if self._group_alive(m.get("pgid")):
                continue                    # still working, leave it alone
            payload = self._collect_result(tdir, None)
            payload.update(state="failed", reason="descendant_exited_no_result",
                           descendant_alive=False, pgid=m.get("pgid"), work_dir=str(tdir))
            self.report(m["task_id"], "failed",
                        f"{m.get('capability', '')} 会话超时后执行体退出且无产物"[:400], payload)
            marker.unlink(missing_ok=True)

    def _too_big(self, payload: dict) -> bool:
        return len(json.dumps(payload, ensure_ascii=False).encode()) > self.MAX_PAYLOAD

    @staticmethod
    def _result_level(payload: dict, code) -> str:
        if payload.get("status") == "failed" or (code is not None and code != 0):
            return "failed"
        return "done"

    def _collect_result(self, tdir: Path, code) -> dict:
        """Pull result.json fully into the payload.

        In the contract, report / acceptance_results / artifacts exist for the platform to read,
        and before 2026-09-17 this only sent back the last stdout line, making the executor's work pointless.
        No more field picking: whatever the executor writes at the top level of result.json comes along
        (report / metrics / evidence ...); that field picking was the root of the "info we wanted stayed local" incident.
        """
        payload: dict = {"exit": code, "work_dir": str(tdir)}
        f = tdir / "result.json"
        if not f.exists():
            return payload
        try:
            r = json.loads(f.read_text())
        except Exception as exc:
            payload["result_error"] = f"result.json 解析失败: {exc}"
            return payload
        if not isinstance(r, dict):
            payload["result_error"] = "result.json 顶层不是对象"
            return payload
        payload.update(r)
        payload["exit"] = code
        payload["work_dir"] = str(tdir)
        payload.setdefault("summary", "")
        payload["artifacts"] = self._ship_artifacts(tdir, r.get("artifacts"))
        if self._too_big(payload):                  # over budget: truncate the body first, then drop evidence
            payload["report"] = str(payload.get("report", ""))[:2000]
            payload["_note"] = "报告过长已截断，完整内容见节点 work_dir/result.json"
        if self._too_big(payload):
            payload.pop("evidence", None)
            payload["_note"] = "payload 过大，evidence 留在节点 work_dir/result.json"
        return payload

    def _ship_artifacts(self, tdir: Path, raw) -> list:
        """Normalize artifacts and mechanically push a copy of small files to the server.

        The executor is not asked to judge "what matters" (the same disease as letting it decide "what to report");
        the rule is dead simple: it's a file, <= ARTIFACT_MAX_BYTES, no explicit upload:false -> upload it.
        Large files and directories keep metadata only, so the platform knows it exists, how big it is, and its shape.
        """
        items = []
        # run.log is included automatically: it is the only raw process evidence, and the executor may not think to list it —
        # yet it is exactly what the platform needs to dig into details later. Trust the rule, not its diligence.
        auto = [{"path": str(p), "name": p.name, "auto": True}
                for p in (tdir / "run.log", tdir / "exec.log") if p.is_file()]
        seen = set()
        for entry in auto + [({"path": a} if isinstance(a, str) else dict(a)) for a in (raw or [])]:
            p = Path(str(entry.get("path", "")))
            if not p.is_absolute():
                p = tdir / str(entry.get("path", ""))
            entry["path"] = str(p)
            if str(p) in seen:
                continue
            seen.add(str(p))
            try:
                if p.is_dir():
                    files = [x for x in p.rglob("*") if x.is_file()]
                    entry.update(exists=True, kind="dir", files=len(files),
                                 bytes=sum(x.stat().st_size for x in files))
                elif p.is_file():
                    entry.update(exists=True, kind="file", bytes=p.stat().st_size)
                    if entry["bytes"] <= self.ARTIFACT_MAX_BYTES and entry.get("upload") is not False:
                        entry["uploaded"] = self._upload(p, tdir, entry)
                else:
                    entry["exists"] = False
            except OSError as exc:
                entry.update(exists=False, error=str(exc))
            items.append(entry)
        return items

    def _upload(self, p: Path, tdir: Path, entry: dict) -> bool:
        try:
            rel = p.relative_to(tdir)
        except ValueError:
            rel = Path(p.name)
        try:
            r = requests.post(f"{self.base}/api/artifact", params={
                "task_id": tdir.name, "node_id": self.node_id,
                "name": str(entry.get("name") or rel),
                "rows": entry.get("rows", ""), "schema": entry.get("schema", ""),
                "local_path": str(p)},
                data=p.read_bytes(), headers={**self.h, "Content-Type": "application/octet-stream"},
                timeout=60)
            if r.status_code < 400:
                entry["artifact_name"] = r.json().get("name", "")
                return True
            print(f"[{ts()}] 产物上传被拒 {r.status_code}: {r.text[:150]}")
        except requests.RequestException as exc:
            print(f"[{ts()}] 产物上传失败 {p.name} ({type(exc).__name__})")
        return False

    def recover_orphans(self):
        """On startup, recover tasks that belong to this node but are not running in this process.

        This used to be a black hole: the process restarted right after claim, the task sat in claimed forever,
        and the platform could not notice without watching heartbeats (two were really lost on 2026-09-17).
        Now we report honestly from local leftovers: if result.json exists, report the result with the saved exit status;
        if nothing exists, report failure honestly. No automatic re-run: a re-run can duplicate side effects.
        """
        try:
            tasks = self._get(f"node/{self.node_id}/tasks",
                              status="claimed,running").get("tasks", [])
        except Exception as exc:
            print(f"[{ts()}] orphan scan failed: {exc}")
            return
        for t in tasks:
            if t["capability"] not in self.cfg["capabilities"]:
                continue
            if self._task_status(t["task_id"]) == "cancelled":
                continue        # do not re-report tasks cancelled upstream; do not let them resurface
            tdir = self.work_root / t["task_id"]
            tdir.mkdir(parents=True, exist_ok=True)
            marker = tdir / "recovered.json"
            if marker.exists():
                continue
            if any(r.get("level") in ("done", "failed") for r in (t.get("reports") or [])):
                continue        # a terminal report already exists; this is just stale state, do not duplicate it
            res_file = tdir / "result.json"
            if res_file.exists():
                state_file = tdir / "run_state.json"
                try:
                    rc = json.loads(state_file.read_text()).get("rc", 0) if state_file.exists() else 0
                except (OSError, ValueError):
                    rc = 1
                payload = self._collect_result(tdir, rc)
                level = self._result_level(payload, rc)
                self.report(t["task_id"], level,
                            (payload.get("summary") or "重启前产出的结果")[:400], payload)
                action = level
            else:
                pending_file = tdir / "pending.json"
                pgid = None
                if pending_file.exists():
                    try:
                        pgid = json.loads(pending_file.read_text()).get("pgid")
                    except Exception:
                        pgid = None
                if self._group_alive(pgid):
                    self.report(t["task_id"], "unresolved",
                                "节点重启后执行体会话仍在后台运行，等待回收"[:400],
                                {"state": "unresolved", "reason": "orphan_descendant_alive",
                                 "descendant_alive": True, "pgid": pgid, "work_dir": str(tdir)})
                    action = "unresolved"
                else:
                    self.report(t["task_id"], "failed",
                                "节点重启导致本次执行中断，本地无产物；需要重新下发"[:400],
                                {"exit": None, "work_dir": str(tdir)})
                    action = "failed"
            marker.write_text(json.dumps({"action": action, "at": datetime.now(CST).isoformat()},
                                         ensure_ascii=False))
            print(f"[{ts()}] 捡回孤儿任务 {t['task_id']} → {action}")

    # ---- Main loop ----
    #
    # Tasks run on background threads while the main loop keeps heartbeating.
    # Otherwise a 40-minute task gets this machine declared offline at minute 31.

    def _free_caps(self) -> list:
        """Capabilities with a free slot, capped by each capability's config concurrency (default 1 = serial)."""
        with self._active_lock:
            running = [a["capability"] for a in self._active.values()]
        out = []
        for c in self.caps:
            limit = int(self.cfg["capabilities"][c].get("concurrency", 1))
            if running.count(c) < limit:
                out.append(c)
        return out

    def _beat_now(self):
        with self._active_lock:
            active = list(self._active.values())
        pending = 0
        if self.outbox.exists():
            pending = len([l for l in self.outbox.read_text().splitlines() if l.strip()])
        tail = f"，outbox {pending} 待补投" if pending else ""
        if active:
            parts = ", ".join(f"{a['capability']}:{int((time.time()-a['t0'])//60)}min" for a in active)
            self.beat(f"running {len(active)} [{parts}]{tail}")
        else:
            self.beat(f"idle{tail}")

    def _start(self, task):
        # 2026-09-24 incident (wsl-kuo0fgi t-20260923-6ee477): we used to register _active, then call _beat_now()
        # (a synchronous HTTP heartbeat), and only then start the worker thread. If that heartbeat timed out or
        # errored (network blip, 502), the exception bubbled to the main loop and got swallowed, so _run_guarded's
        # finally never ran — the task never actually started, yet the concurrency slot was held forever (task stuck
        # in claimed, no progress reported, never reached watch_cancel), even close_task upstream could not clear it,
        # and no later queued task could get a slot. Fix: start the thread first (its finally releases the slot),
        # then do a best-effort heartbeat; if the thread cannot start, roll back the registration — no ownerless ghost slot.
        with self._active_lock:
            self._active[task["task_id"]] = {"capability": task["capability"], "t0": time.time()}
        try:
            threading.Thread(target=self._run_guarded, args=(task,), daemon=True).start()
        except Exception as exc:
            with self._active_lock:
                self._active.pop(task["task_id"], None)
            print(f"[{ts()}] 工作线程启动失败，已回滚槽位 {task['task_id']}: {exc}")
            raise
        try:
            self._beat_now()
        except Exception as exc:
            print(f"[{ts()}] 槽位已就绪，心跳补发失败（best-effort，不回收槽位）: {exc}")

    def _run_guarded(self, task):
        try:
            self.run_task(task)
        except Exception as exc:
            print(f"[{ts()}] task error: {exc}")
            try:
                tdir = self.work_root / task["task_id"]
                if self._has_result(tdir):          # even on an exception, harvest any result/artifacts already produced
                    info = {"pgid": None, "rc": None, "elapsed": 0, "timeout_sec": None}
                    payload = self._harvest(tdir, info, None, "harvested", "exception_path")
                    summary = (payload.get("summary") or "异常退出前已产出结果").strip()
                    self.report(task["task_id"], self._result_level(payload, None), summary[:400], payload)
                else:
                    self.report(task["task_id"], "failed", f"执行体异常: {exc}"[:400],
                                self._collect_result(tdir, None))
            except Exception:
                pass
        finally:
            with self._active_lock:
                self._active.pop(task["task_id"], None)

    def loop(self):
        self.register()
        self.flush_outbox()        # retry reports that failed to send last time
        self.recover_orphans()     # honestly report tasks left unfinished last time, from local leftovers
        self.sweep_pending()       # harvest descendants handed to the background by a session timeout
        poll = int(self.cfg.get("poll_sec", 20))
        last_beat = 0
        last_reg = time.time()
        while True:
            try:
                if time.time() - last_beat > 60:
                    self._beat_now()
                    last_beat = time.time()

                self.flush_outbox()
                self.sweep_pending()               # collect descendants that finished in the background after a timeout

                if time.time() - last_reg > 600:   # re-check whether the skeleton is stale every 10 minutes
                    try:
                        self.register()
                    except Exception as exc:
                        print(f"[{ts()}] re-register failed: {exc}")
                    last_reg = time.time()

                started = False
                caps = self._free_caps()           # claim only capabilities with free slots -> parallel tasks on one machine
                if caps:
                    task = self.claim(caps)
                    if task:
                        self._start(task)
                        started = True

                if started:
                    continue                       # immediately fill the next free slot
                time.sleep(5 if self._active else poll)
            except KeyboardInterrupt:
                print("bye")
                return
            except Exception as exc:
                print(f"[{ts()}] loop error: {exc}")
                time.sleep(poll)


def load_config(path) -> dict:
    if not os.environ.get("PAD_KEY"):
        keyfile = Path.home() / ".pad_key"
        if keyfile.exists():
            os.environ["PAD_KEY"] = keyfile.read_text().strip()
    raw = yaml.safe_load(Path(path).read_text())

    def expand(v):
        if isinstance(v, str):
            return re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), v)
        if isinstance(v, dict):
            return {k: expand(x) for k, x in v.items()}
        if isinstance(v, list):
            return [expand(x) for x in v]
        return v

    return expand(raw)


if __name__ == "__main__":
    LD(load_config(sys.argv[1])).loop()
