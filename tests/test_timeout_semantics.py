#!/usr/bin/env python3
"""回归测试：executor/session 的 hard timeout 不得被等同于业务 Task 失败。

覆盖 2026-09-18 真实事故（节点 A t-20260918-6a7222 / 节点 B t-20260917-a83e54）：
capability 的会话超时只 kill 了外层 sh，run_harness(孙)/claude(曾孙) 继续跑完并写出
result.json，旧代码却在 TimeoutExpired 分支直接 report(failed) 且跳过 _collect_result()。

用真实 subprocess 复现「wrapper 超时但 descendant 继续完成」，不 mock 进程生命周期。

跑法：
    cd "$(git rev-parse --show-toplevel)" && .venv/bin/python -m unittest discover -s tests -v
"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ld_agent as ld  # noqa: E402


GRANDCHILD_SRC = """\
import json, os, sys, time
time.sleep(float(sys.argv[1]))
wd = os.environ["PAD_WORK_DIR"]
open(os.path.join(wd, "result.json"), "w").write(json.dumps(
    {"summary": "grandchild (executor) finished",
     "report": "descendant completed after wrapper timed out"}))
"""

WRAPPER_SRC = """\
import os, subprocess, sys
# 模拟 run_harness.py：把真正的干活进程作为孙/曾孙拉起来后等待
subprocess.run([sys.executable, sys.argv[1], sys.argv[2]], check=False)
"""

SLOW_WITH_RESULT_SRC = """\
import json, os, time
wd = os.environ["PAD_WORK_DIR"]
open(os.path.join(wd, "result.json"), "w").write(json.dumps(
    {"summary": "already computed",
     "report": "result existed before session timeout",
     "artifacts": [{"name": "data.csv", "path": "data.csv", "rows": 3,
                    "schema": "a,b", "upload": True}]}))
open(os.path.join(wd, "data.csv"), "w").write("a,b\\n1,2\\n")
time.sleep(20)
"""

OK_SRC = """\
import json, os
wd = os.environ["PAD_WORK_DIR"]
open(os.path.join(wd, "result.json"), "w").write(json.dumps({"summary": "ok done"}))
print("ok")
"""

FAIL_SRC = """\
import sys
sys.stdout.write("boom\\n")
sys.exit(2)
"""


class FakeLD(ld.LD):
    """不碰网络的 LD：记录 report/upload，测试用秒级超时。"""

    LEASE_POLL_SEC = 0.2

    def __init__(self, cfg):
        super().__init__(cfg)
        self.reports = []
        self.uploads = []

    def _timeout_seconds(self, spec) -> int:
        return int(spec.get("timeout_sec", 1))

    def _lease_seconds(self, spec) -> int:
        return int(spec.get("lease_sec", 3))

    def report(self, task_id, level, summary, payload=None, client_id=None):
        self.reports.append({"task_id": task_id, "level": level,
                             "summary": summary, "payload": payload or {}})

    def _upload(self, p, tdir, entry):
        self.uploads.append(str(p))
        entry["uploaded"] = True
        return True

    def last(self):
        return self.reports[-1] if self.reports else None

    def levels(self):
        return [r["level"] for r in self.reports]


class TimeoutSemanticsTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.fixtures = Path(__file__).resolve().parent / "fixtures"
        self.fixtures.mkdir(exist_ok=True)
        for name, src in (("grandchild.py", GRANDCHILD_SRC), ("wrapper.py", WRAPPER_SRC),
                          ("slow_with_result.py", SLOW_WITH_RESULT_SRC),
                          ("ok.py", OK_SRC), ("fail.py", FAIL_SRC)):
            (self.fixtures / name).write_text(src)

    def tearDown(self):
        self.tmp.cleanup()

    def make_ld(self, cmd, **spec):
        cap_spec = {"cmd": cmd, "timeout_min": 4320}
        cap_spec.update(spec)
        cfg = {"node_id": "test-1", "capabilities": {"cap": cap_spec},
               "pad_url": "http://localhost:1", "pad_key": "k",
               "work_dir": str(self.root)}
        return FakeLD(cfg)

    def make_task(self, tid="t-test-1"):
        return {"task_id": tid, "capability": "cap", "goal": "g",
                "constraints": [], "acceptance": ["a1"]}

    def py(self, name):
        return f"{sys.executable} {self.fixtures / name}"

    # ---- 1. 正常完成 -------------------------------------------------------
    def test_normal_completion_reports_done(self):
        L = self.make_ld(self.py("ok.py"), timeout_sec=5)
        L.run_task(self.make_task())
        self.assertEqual(L.last()["level"], "done")
        self.assertEqual(L.last()["payload"].get("summary"), "ok done")

    # ---- 2. wrapper 超时但子任务继续（事故复现）----------------------------
    def test_wrapper_timeout_child_continues_is_not_failed(self):
        cmd = f"{sys.executable} {self.fixtures / 'wrapper.py'} " \
              f"{self.fixtures / 'grandchild.py'} 2"
        L = self.make_ld(cmd, timeout_sec=1, lease_sec=4)
        L.run_task(self.make_task())
        self.assertNotIn("failed", L.levels(),
                         "会话超时不得把业务 Task 判 failed")
        self.assertEqual(L.last()["level"], "done",
                         "descendant 完成后必须 harvest 成 done")
        self.assertIn("grandchild", L.last()["payload"].get("summary", ""))
        self.assertEqual(L.last()["payload"].get("state"), "harvested")

    # ---- 3. 超时后已有 result/artifact ------------------------------------
    def test_timeout_with_existing_result_harvests_artifacts(self):
        # 给足 lease，确保走「发现 result 就收口」而不是 unresolved
        L = self.make_ld(self.py("slow_with_result.py"), timeout_sec=1, lease_sec=3)
        L.run_task(self.make_task())
        self.assertNotIn("failed", L.levels())
        self.assertEqual(L.last()["level"], "done")
        self.assertEqual(L.last()["payload"].get("summary"), "already computed")
        self.assertTrue(any(p.endswith("data.csv") for p in L.uploads),
                        "已存在的 artifact 必须被回收上传")
        arts = L.last()["payload"].get("artifacts") or []
        self.assertTrue(any(a.get("name") == "data.csv" for a in arts))

    # ---- 4. 真正执行失败 ---------------------------------------------------
    def test_genuine_failure_reports_failed(self):
        L = self.make_ld(self.py("fail.py"), timeout_sec=5)
        L.run_task(self.make_task())
        self.assertEqual(L.last()["level"], "failed")

    # ---- 5. descendant 熬过 lease：unresolved，随后 sweep 回收为 done ------
    def test_descendant_outlives_lease_then_sweep_harvests(self):
        cmd = f"{sys.executable} {self.fixtures / 'wrapper.py'} " \
              f"{self.fixtures / 'grandchild.py'} 3"
        L = self.make_ld(cmd, timeout_sec=1, lease_sec=1)
        task = self.make_task("t-lease")
        L.run_task(task)
        self.assertNotIn("failed", L.levels())
        self.assertEqual(L.last()["level"], "unresolved")
        self.assertTrue(L.last()["payload"].get("descendant_alive"))
        self.assertTrue((self.root / "t-lease" / "pending.json").exists())
        # 等 descendant 实际写完，再 sweep
        time.sleep(3.5)
        L.sweep_pending()
        self.assertEqual(L.last()["level"], "done")
        self.assertFalse((self.root / "t-lease" / "pending.json").exists())

    # ---- 6. 异常路径也要 harvest 已有 result ------------------------------
    def test_exception_path_harvests_existing_result(self):
        L = self.make_ld(self.py("ok.py"), timeout_sec=5)
        task = self.make_task("t-exc")
        tdir = self.root / "t-exc"
        tdir.mkdir(parents=True, exist_ok=True)
        (tdir / "result.json").write_text(json.dumps({"summary": "produced before crash"}))

        def boom(_task):
            raise RuntimeError("simulated executor wrapper crash")

        L.run_task = boom
        L._run_guarded(task)
        self.assertEqual(L.last()["level"], "done")
        self.assertEqual(L.last()["payload"].get("summary"), "produced before crash")

    # ---- 7. claim 后心跳失败不得泄漏并发槽（2026-09-24 真实事故）----
    def test_start_heartbeat_failure_does_not_leak_slot(self):
        """_start 里同步心跳失败时，任务线程必须照常起、槽位必须照常释放。

        旧实现顺序是 _active 登记 → _beat_now() → 起线程：心跳异常会冒到主循环被吞，
        _run_guarded 的 finally 永不执行，任务从没跑却永久占住并发槽（6ee477 事故）。
        """
        L = self.make_ld(self.py("ok.py"), timeout_sec=5)

        def boom():
            raise RuntimeError("simulated heartbeat network failure")

        L._beat_now = boom
        task = self.make_task("t-beat")
        L._start(task)
        for _ in range(200):
            with L._active_lock:
                if not L._active:
                    break
            time.sleep(0.05)
        with L._active_lock:
            self.assertEqual(L._active, {}, "心跳失败不得留下没有工作线程的幽灵槽位")
        self.assertEqual(L.last()["level"], "done", "任务线程必须照常跑完并汇报")

    # ---- 8. 默认值/兼容性 --------------------------------------------------
    def test_defaults_and_compat(self):
        cfg = {"node_id": "n", "capabilities": {"cap": {"cmd": "true"}},
               "pad_url": "http://localhost:1", "pad_key": "k",
               "work_dir": str(self.root)}
        L = ld.LD(cfg)
        self.assertEqual(L._timeout_seconds({}), 4320 * 60)      # 旧默认 4320 未变
        self.assertEqual(L._lease_seconds({}), 60 * 60)          # 新 lease 默认 60min
        self.assertEqual(L.work_root, self.root)
        # 服务端 report() 对非 progress/done/failed 的 level 只记录、不改任务状态
        self.assertNotIn("unresolved", ("progress", "done", "failed"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
