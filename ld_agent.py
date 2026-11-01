"""Product LD agent —— 每台机器跑一个。

它做四件事：
  1. 向 PAD 注册自己的能力（capability）
  2. 轮询领任务（按能力，不按机器）
  3. 跑活，把结果整理成「汇报」回传
  4. 执行体自行探索并完成任务；最终将 done / failed 报告写回 PAD

用法:
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
    # 单条汇报 payload 的上限（服务端实测能吃 ≥12KB；留余量，超了降级而不是丢）
    MAX_PAYLOAD = 16000
    # 产物自动上传的单文件上限；超了就只留本地 + 元数据
    ARTIFACT_MAX_BYTES = 5 * 1024 * 1024
    # 会话超时后进入 lease 观察：继续等/收仍在跑的 descendant；到期仍活就报 unresolved
    # （任务保持非终态，交给 sweep/recover_orphans 继续收），绝不因为会话超时判业务失败。
    DEFAULT_LEASE_MIN = 60
    LEASE_POLL_SEC = 10
    # 执行体运行期间轮询 PAD 任务状态的间隔：发现 cancelled 就杀进程组（取消传播）
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
        self.outbox = self.work_root / "outbox.jsonl"   # 投不出去的汇报先落这儿，主循环补投
        self._out_lock = threading.Lock()
        self._active = {}                               # 正在跑：task_id -> {capability, t0}
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

    # ---- 报告的投递（不许静默丢） ----
    #
    # 2026-09-17 事故：report() 直接 POST，网关 502 时异常被上层 except 吞掉，
    # 结果就是「活干完了、本地有产物、上层永远不知道」。更根本的是：5/5 都返回 200，
    # 服务端存的却是空壳 payload —— 只信 ack 不够。
    # 所以现在：POST → **读回确认**（task_view 里能不能查到这条 client_id）→
    # 确认不到就重投 → 仍不行落 outbox，主循环每轮补投，直到线上真的有了为止。
    # 每条带 client_id，服务端按它去重（重投/重启后重投都不会产生重复汇报）。

    def _attempt(self, path: str, body: dict, quiet: bool = False) -> tuple[str, dict | None]:
        """一次投递尝试。ok=已确认落库；reject=服务端明确拒绝（重投无用）；retry=再试。"""
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
        """读回：这条到底有没有进线上状态。查不到就是没送到，别自欺。"""
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
            return False                      # 服务端会存 client_id，却没这条 → 真没到
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
            if len(left) != getattr(self, "_out_pending", -1):   # 只在数字变化时吭声
                print(f"[{ts()}] outbox 待补投 {len(left)}/{len(lines)} 条")
                self._out_pending = len(left)

    # ---- 基础动作 ----

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
        """领一个活。capabilities 只传「当前还有空槽」的能力，避免领回来跑不了。"""
        caps = list(self.caps if capabilities is None else capabilities)
        if not caps:
            return None
        return self._post("claim", {"node_id": self.node_id, "capabilities": caps})["task"]

    INPUT_FILE_MAX_BYTES = 10 * 1024 * 1024
    INPUT_FILE_MAX_COUNT = 8

    def _receive_input_files(self, task, tdir: Path) -> list[dict]:
        """把随任务下发的输入文件下载到本机任务目录，并核对字节数与 SHA-256。

        服务端把「图片」和「普通文件」放在同一条清单里（input_files，旧名 input_images），
        这里一律按字节处理：图片/PDF/CSV/zip 都只是文件，能否理解由执行体自己决定。
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

    # 老名字：外部脚本/文档提过这个方法名。
    _receive_input_images = _receive_input_files

    def report(self, task_id, level, summary, payload=None, client_id=None):
        payload = payload if payload is not None else {}
        if client_id is None:
            if level == "progress":
                client_id = uuid.uuid4().hex
            else:
                # 终态汇报用确定性 id：重投、outbox 补投、重启后重投都只会算一条
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

    # ---- 干活 ----

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
            # 旧变量名保留：老的 capability 命令/脚本里有读它们的。
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

        if info.get("cancelled"):                   # 上层取消：执行体已杀，不报终态
            self._on_cancelled(task, tdir, info)
            return

        if info["timed_out"]:                       # 会话超时 ≠ 任务失败，走回收语义
            self._on_session_timeout(task, tdir, info, spec)
            return

        payload = self._collect_result(tdir, info["rc"])
        if self._result_level(payload, info["rc"]) == "done":
            summary = (payload.get("summary") or "").strip() or (self._tail(tdir) or f"{cap} 完成")
            self.report(task["task_id"], "done", summary[:400], payload)
        else:
            summary = (payload.get("summary") or self._tail(tdir) or "任务未完成").strip()
            self.report(task["task_id"], "failed", summary[:400], payload)

    # ---- 单次 executor/session 生命周期：与业务 Task 状态解耦 ----
    #
    # 2026-09-18 事故（wsl-kuo0fgi t-20260918-6a7222 / mac-1 t-20260917-a83e54）：
    # capability 的 60min timeout 只 kill 了外层 sh；run_harness(孙) 与 claude/opencode(曾孙)
    # 被 init 收养后继续跑了 2h01m，完整产出 result.json + 16 个 artifacts，但旧代码在
    # TimeoutExpired 分支直接 report(failed) 并 return，**跳过了 _collect_result()**，
    # 于是「活干完了、PAD 只看到失败」。根因：把单次 wrapper 的 hard timeout 当成业务 Task failed。
    # 修复原则：
    #   1) 会话超时后一律先 harvest，有 result 就按结果收口；
    #   2) descendant 仍活着就进 lease 观察，到期仍活报 unresolved（任务保持非终态，可回收），
    #      绝不 kill 仍可能完成任务的有效 descendant；
    #   3) 只有「executor 已退出且无任何 result」才算真失败。
    # 兼容：capability/调用方式不变；timeout_min 仍是会话软超时；lease_min 可选，缺省 60。

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
        """查任务当前状态；网络异常返回 None（拿不到状态就不乱杀）。"""
        try:
            return self._get(f"task/{task_id}/status").get("status")
        except Exception:
            return None

    def _kill_group(self, pgid, grace=None) -> None:
        """先 TERM 再 KILL，整个进程组一起收（executor 的 descendant 都在这一组）。"""
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
        """在独立进程组里跑一次 executor。

        超时**不**杀进程组：descendant 可能仍在完成有效工作（事故已验证）。
        但上层把任务 cancelled 时必须真的停下来 —— 运行期间轮询状态，发现取消就杀整组。
        输出落 exec.log（文件而非管道），避免孤儿进程写满管道被阻塞。
        """
        logf = (tdir / "exec.log").open("w")
        t0 = time.time()
        proc = subprocess.Popen(cmd, shell=True, cwd=str(tdir), env=env,
                                stdout=logf, stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        pgid = proc.pid                     # start_new_session ⇒ 子进程即会话/进程组组长
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
            logf.close()                    # 关父进程这份；子进程仍持有自己的 fd
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
        """有 result.json 就按报告状态收口（会话超时/异常/重启补收都走这里）。"""
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
        """任务被上层取消：执行体已终止，别报 done/failed（控制面已定终态），清掉可复活标记。"""
        (tdir / "pending.json").unlink(missing_ok=True)
        (tdir / "cancelled.json").write_text(json.dumps({
            "task_id": task["task_id"], "capability": task["capability"],
            "pgid": info.get("pgid"), "at": datetime.now(CST).isoformat(),
        }, ensure_ascii=False))
        print(f"[{ts()}] {task['task_id']} 取消完成：执行体已终止，槽位释放")

    def _on_session_timeout(self, task, tdir, info, spec) -> None:
        cap = task["capability"]
        # 1) 已有 result：活其实干完了，按结果收口，别报失败
        if self._has_result(tdir):
            self._finish_from_work(task, tdir, info, "session_timeout_result_present")
            return
        # 2) descendant 还活着：lease 观察，期间持续 harvest，绝不 kill
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
                # 到期仍在跑：报 unresolved（任务保持非终态），落 marker，交给 sweep 继续收
                self._write_pending(tdir, task, info, info["timeout_sec"], lease_s)
                payload = {"state": "unresolved", "reason": "executor_session_timeout",
                           "descendant_alive": True, "pgid": info.get("pgid"),
                           "session_timeout_sec": info["timeout_sec"], "lease_sec": lease_s,
                           "elapsed_sec": info.get("elapsed"), "work_dir": str(tdir),
                           "summary": f"{cap} 会话超时，执行体仍在运行"}
                self.report(task["task_id"], "unresolved",
                            f"{cap} 会话超时，执行体仍在运行（已转后台回收）"[:400], payload)
                return
        # 3) descendant 已退出且无 result：真正的执行失败（仍带上已存在的 artifacts）
        payload = self._collect_result(tdir, info.get("rc"))
        payload.update(state="failed", reason="executor_exited_no_result",
                       descendant_alive=False, pgid=info.get("pgid"), work_dir=str(tdir))
        self.report(task["task_id"], "failed",
                    (self._tail(tdir) or f"{cap} 会话超时且执行体已退出、无产物")[:400], payload)

    def sweep_pending(self) -> None:
        """会话超时转后台的 descendant：有结果就收，死了没结果才算失败。主循环每轮调用。"""
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
            if self._task_status(m["task_id"]) == "cancelled":   # 上层已取消，别复活
                marker.unlink(missing_ok=True)
                continue
            if self._has_result(tdir):
                self._finish_from_work(task, tdir, info, "harvested_by_sweep")
                marker.unlink(missing_ok=True)
                continue
            if self._group_alive(m.get("pgid")):
                continue                    # 还在干，别打扰
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
        """把 result.json 整个收进 payload。

        契约里 report / acceptance_results / artifacts 就是给上层看的，
        2026-09-17 之前这里只回传 stdout 最后一行，等于让执行体白写。
        现在不再挑字段：执行体在 result.json 顶层写什么就带什么（report / metrics / evidence …），
        挑字段正是"想知道的信息丢在本地"那次事故的根。
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
        if self._too_big(payload):                  # 超限先截正文，再退掉 evidence
            payload["report"] = str(payload.get("report", ""))[:2000]
            payload["_note"] = "报告过长已截断，完整内容见节点 work_dir/result.json"
        if self._too_big(payload):
            payload.pop("evidence", None)
            payload["_note"] = "payload 过大，evidence 留在节点 work_dir/result.json"
        return payload

    def _ship_artifacts(self, tdir: Path, raw) -> list:
        """归一化 artifacts，并机械地把小文件存一份到服务端。

        不让执行体判断"什么算重要"（那和它自己决定"汇报什么"是同一个毛病），
        规则是死的：是文件、≤ ARTIFACT_MAX_BYTES、没显式写 upload:false → 就传。
        大的/目录只留元数据，让上层知道东西在、多大、什么结构。
        """
        items = []
        # run.log 自动带一份：这是唯一"过程原始证据"，执行体不一定会想到列它，
        # 而它正是上层事后想追细节时最该有的东西。靠规则，不靠它的自觉。
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
        """开机捡回「自己名下但这次进程没在跑」的任务。

        以前这种情况是黑洞：claim 之后进程被重启，任务永远停在 claimed，线上不看
        心跳根本发现不了（2026-09-17 真丢过两条）。现在按本地残留如实补交：
        有 result.json 就按保存的退出状态补交结果；什么都没有就如实报失败。
        不自动重跑，重跑可能造成重复副作用。
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
                continue        # 上层已取消的任务不补交，别再冒出来
            tdir = self.work_root / t["task_id"]
            tdir.mkdir(parents=True, exist_ok=True)
            marker = tdir / "recovered.json"
            if marker.exists():
                continue
            if any(r.get("level") in ("done", "failed") for r in (t.get("reports") or [])):
                continue        # 已有终态汇报，只是状态残留，别重复补交
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

    # ---- 主循环 ----
    #
    # 任务跑在后台线程里，主循环继续心跳。
    # 不然一个跑 40 分钟的任务会让这台机器在第 31 分钟被误判成失联。

    def _free_caps(self) -> list:
        """还有空槽的能力名。每个能力的并发上限来自配置 concurrency（默认 1 = 串行）。"""
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
        # 2026-09-24 事故（wsl-kuo0fgi t-20260923-6ee477）：原来先登记 _active、再 _beat_now()
        # （同步 HTTP 心跳）、最后才起工作线程。心跳一旦超时/报错（网络抖动、502），异常提前
        # 冒到主循环被吞掉，_run_guarded 的 finally 永远不会执行 —— 这个任务从没真正开跑，
        # 却把并发槽永久占住（任务一直停在 claimed、没有 progress 上报、也不进 watch_cancel），
        # 上层 close_task 也清不掉它，后续 queued 任务再无槽可领。
        # 修复：先起线程（槽位由线程的 finally 负责释放），再补一次 best-effort 心跳；
        #        线程都起不来就回滚登记，绝不留下“有槽无主”的幽灵占用。
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
                if self._has_result(tdir):          # 异常退出也要 harvest 已产出的 result/artifacts
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
        self.flush_outbox()        # 上次没送到的汇报，先补投
        self.recover_orphans()     # 上次没跑完的任务，按本地残留如实补交
        self.sweep_pending()       # 会话超时转后台的 descendant，有结果就收
        poll = int(self.cfg.get("poll_sec", 20))
        last_beat = 0
        last_reg = time.time()
        while True:
            try:
                if time.time() - last_beat > 60:
                    self._beat_now()
                    last_beat = time.time()

                self.flush_outbox()
                self.sweep_pending()               # 回收会话超时后仍在后台完成的 descendant

                if time.time() - last_reg > 600:   # 10 分钟复核一次骨架是否过期
                    try:
                        self.register()
                    except Exception as exc:
                        print(f"[{ts()}] re-register failed: {exc}")
                    last_reg = time.time()

                started = False
                caps = self._free_caps()           # 只领还有空槽的能力 → 同机多任务并行
                if caps:
                    task = self.claim(caps)
                    if task:
                        self._start(task)
                        started = True

                if started:
                    continue                       # 立刻再填下一个空槽
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
