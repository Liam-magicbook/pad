"""并发抓取生产 PAD 的任务详情，用于论文的实测数据。

    KEY=$(cat ~/.pad_key) python3 tools/collect_stats.py --out paper/data/tasks.json
"""
import argparse
import collections
import json
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor

import requests

def fetch(base, tid, session=None):
    s = session or requests
    try:
        v = s.get(f"{base}/api/task/{tid}", timeout=30).json()
    except Exception:
        return None
    return v if isinstance(v, dict) and "error" not in v else None


def summary(rows):
    st = collections.Counter(r["status"] for r in rows)
    by_cap = collections.Counter(r["capability"] for r in rows)
    by_node = collections.Counter(r["node_id"] for r in rows if r.get("node_id"))
    # 时长：created_at → updated_at（含排队）
    durs = []
    for r in rows:
        try:
            from datetime import datetime
            a = datetime.fromisoformat(r["created_at"])
            b = datetime.fromisoformat(r["updated_at"])
            durs.append((b - a).total_seconds())
        except Exception:
            pass
    out = {
        "total": len(rows),
        "status": dict(st),
        "by_capability": dict(by_cap.most_common(30)),
        "by_node": dict(by_node),
        "duration_sec": {
            "n": len(durs),
            "p50": round(statistics.median(durs), 1) if durs else None,
            "p90": round(statistics.quantiles(durs, n=10)[8], 1) if len(durs) > 9 else None,
            "p99": round(statistics.quantiles(durs, n=100)[98], 1) if len(durs) > 99 else None,
            "max": round(max(durs), 1) if durs else None,
        },
        "with_reports": sum(1 for r in rows if r.get("reports")),
        "with_acceptance": sum(
            1 for r in rows
            if any((x.get("payload") or {}).get("acceptance_results") for x in (r.get("reports") or []))),
        "with_artifacts": sum(1 for r in rows if r.get("artifacts")),
        "with_input_files": sum(1 for r in rows if r.get("input_files")),
        "resumed_after_cancel": sum(1 for r in rows if r["status"] == "cancelled"),
    }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=os.environ.get("PAD_URL", ""))
    ap.add_argument("--key-file", default=os.path.expanduser("~/.pad_key"))
    ap.add_argument("--out", default="paper/data/tasks.json")
    ap.add_argument("--workers", type=int, default=12)
    a = ap.parse_args()

    key = open(a.key_file).read().strip() if os.path.exists(a.key_file) else os.environ.get("PAD_KEY", "")
    base = (a.url or os.environ.get("PAD_URL", "")).rstrip("/")
    if not base:
        sys.exit("给 --url 或 PAD_URL（如 http://127.0.0.1:8790/pad/<KEY>）")
    if "/pad/" not in base:
        base = f"{base}/pad/{key}"

    d = requests.get(f"{base}/api/digest", params={"recent_limit": 500}, timeout=60).json()
    ids = sorted({t["task_id"] for n in d["nodes"] for t in (n.get("recent_tasks") or [])})
    print(f"发现 {len(ids)} 个任务，抓取中…", file=sys.stderr)

    rows = []
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, v in enumerate(ex.map(lambda t: fetch(base, t), ids)):
            if v:
                rows.append(v)
            if i % 100 == 0:
                print(f"  {i}/{len(ids)}  {len(rows)} ok", file=sys.stderr)

    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    json.dump({"digest": d, "tasks": rows}, open(a.out, "w"), ensure_ascii=False, indent=1)
    print(json.dumps(summary(rows), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
