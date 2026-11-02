#!/usr/bin/env python3
"""Compress the raw scrape from collect_stats.py into the paper's dataset,
**aggregates only**.

The raw file contains real goal text and task ids (operator data), so it cannot go into a
public repository. This script keeps only counts, percentiles and classification results and
writes paper/data/aggregates.json.

    python3 tools/make_paper_data.py paper/data/tasks.json paper/data/aggregates.json
"""
import collections
import json
import re
import statistics
import sys
from datetime import datetime

PATTERNS = [
    ("Executor environment", r"CLI|executor|opencode|claude|codex|not found|PATH"
                             r"|command not found|No such file"),
    ("Timeout / interruption", r"timeout|超时|中断|killed|Interrupt|SIGTERM"),
    ("Credentials or authorization", r"cookie|token|auth|401|403|凭据|登录|授权|forbidden"),
    ("Resource exhaustion", r"disk|no space|内存|存储|OOM|space left"),
    ("Network / upstream", r"network|connection|HTTP [45]|429|502|503|rate limit|ECONN"),
    ("Goal not achievable", r"未完成|未达|not done|放弃|无法|不能|blocked|impossible"
                             r"|0/\d|no compliant source"),
]


def t(s):
    return datetime.fromisoformat(s)


def pct(xs, q):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * q))] if xs else None


def main(src="paper/data/tasks.json", dst="paper/data/aggregates.json"):
    raw = json.load(open(src))
    rows = raw["tasks"]
    work = [r for r in rows if "ping.self" not in r["capability"]]

    def dur(r):
        return (t(r["updated_at"]) - t(r["created_at"])).total_seconds()

    def has_evidence(r):
        return any((x.get("payload") or {}).get("acceptance_results")
                   for x in (r.get("reports") or []))

    out = {"generated_from": "live PAD deployment", "n_raw_tasks": len(rows),
           "n_work_tasks": len(work)}

    # fleet
    fleet = []
    for n in raw["digest"]["nodes"]:
        mine = [r for r in work if r.get("node_id") == n["node_id"]]
        fleet.append({
            "node": n["node_id"],
            "n_capabilities": len(n["capabilities"]),
            "machine_hours": round(sum(dur(r) for r in mine) / 3600, 1),
            "n_tasks": len(mine),
            "role": n["role"],
        })
    out["fleet"] = sorted(fleet, key=lambda x: -x["machine_hours"])
    out["total_machine_hours"] = round(sum(f["machine_hours"] for f in fleet), 1)
    out["n_capabilities"] = len({c for n in raw["digest"]["nodes"] for c in n["capabilities"]})

    # status
    out["status"] = dict(collections.Counter(r["status"] for r in work))

    # duration buckets
    def bucket(s):
        if s < 600:
            return "<10m"
        if s < 3600:
            return "10m-1h"
        if s < 6 * 3600:
            return "1-6h"
        return ">6h"

    buckets = {}
    for b in ["<10m", "10m-1h", "1-6h", ">6h"]:
        sub = [r for r in work if bucket(dur(r)) == b]
        done = [r for r in sub if r["status"] == "done"]
        buckets[b] = {
            "n": len(sub),
            "done": len(done),
            "failed": sum(1 for r in sub if r["status"] == "failed"),
            "cancelled": sum(1 for r in sub if r["status"] == "cancelled"),
            "evidence_pct": round(100 * sum(1 for r in done if has_evidence(r)) / max(1, len(done))),
        }
    out["duration_buckets"] = buckets
    durs = [dur(r) for r in work]
    out["duration_sec"] = {"p50": pct(durs, .5), "p90": pct(durs, .9),
                           "p99": pct(durs, .99), "max": max(durs)}
    out["over_1h"] = sum(1 for x in durs if x > 3600)
    out["over_6h"] = sum(1 for x in durs if x > 21600)

    # queue vs execution
    q, run = [], []
    for r in work:
        rs = r.get("reports") or []
        if not rs:
            continue
        q.append((t(rs[0]["at"]) - t(r["created_at"])).total_seconds())
        if len(rs) > 1:
            run.append((t(rs[-1]["at"]) - t(rs[0]["at"])).total_seconds())
    out["queue_sec"] = {"p50": pct(q, .5), "p90": pct(q, .9), "p99": pct(q, .99)}
    out["exec_sec"] = {"p50": pct(run, .5), "p90": pct(run, .9), "p99": pct(run, .99)}

    # evidence
    done = [r for r in work if r["status"] == "done"]
    failed = [r for r in work if r["status"] == "failed"]
    out["evidence"] = {
        "done": len(done),
        "done_with_evidence": sum(1 for r in done if has_evidence(r)),
        "failed": len(failed),
        "failed_with_evidence": sum(1 for r in failed if has_evidence(r)),
        "failed_with_artifacts": sum(1 for r in failed if r.get("artifacts")),
        "mean_failed_report_chars": round(statistics.mean(
            [len(str((x.get("payload") or {}).get("report", "")))
             for r in failed for x in (r.get("reports") or []) if x["level"] == "failed"]
        ) or 0),
    }

    def chk(r):
        for x in reversed(r.get("reports") or []):
            p = x.get("payload") or {}
            if "_acceptance_check" in p:
                return p["_acceptance_check"]
        return None

    has = [r for r in rows if r.get("acceptance")]
    out["acceptance"] = {
        "tasks_with_criteria": len(has),
        "answered_any": sum(1 for r in has if has_evidence(r)),
        "no_gap": sum(1 for r in has if (c := chk(r)) and c["missing"] == []),
    }

    # cancellation
    can = [r for r in rows if r["status"] == "cancelled"]
    gaps = []
    for r in can:
        rs = [x for x in (r.get("reports") or []) if x["level"] == "progress"]
        if rs:
            gaps.append((t(r["updated_at"]) - t(rs[-1]["at"])).total_seconds() / 60)
    out["cancellation"] = {
        "n": len(can),
        "with_progress": sum(1 for r in can if "progress" in [x["level"] for x in (r.get("reports") or [])]),
        "gap_min_p50": round(statistics.median(gaps), 1) if gaps else None,
        "gap_min_max": round(max(gaps), 1) if gaps else None,
    }

    # failure taxonomy (all failed tasks, first-match)
    tax = collections.Counter()
    for r in rows:
        if r["status"] != "failed":
            continue
        text = " ".join(str(x.get("summary", "")) for x in (r.get("reports") or []))
        for name, pat in PATTERNS:
            if re.search(pat, text, re.I):
                tax[name] += 1
                break
        else:
            tax["Unclassified"] += 1
    out["failure_taxonomy"] = dict(tax.most_common())
    out["failure_taxonomy_total"] = sum(tax.values())

    json.dump(out, open(dst, "w"), ensure_ascii=False, indent=2)
    print(json.dumps(out, ensure_ascii=False, indent=2)[:2000])
    print(f"\n→ {dst}  ({len(open(dst).read())} bytes)")


if __name__ == "__main__":
    main(*sys.argv[1:])
