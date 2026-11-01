"""把失败任务按终端汇报里的主因归类（论文 §6 的 Table）。

    python3 tools/classify_failures.py paper/data/tasks.json

规则是"第一个命中的模式即主因"，顺序固定，这样同一份数据永远给出同一张表。
分类不完全可靠——它读的是终态 summary 的文字——所以论文里把它当作"主因分布"，
不作为精确因果。
"""
import collections
import json
import re
import sys

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


def main(path="paper/data/tasks.json"):
    data = json.load(open(path))
    fails = [r for r in data["tasks"] if r["status"] == "failed"]
    cnt = collections.Counter()
    for r in fails:
        text = " ".join(str(x.get("summary", "")) for x in (r.get("reports") or []))
        for name, pat in PATTERNS:
            if re.search(pat, text, re.I):
                cnt[name] += 1
                break
        else:
            cnt["Unclassified"] += 1

    total = sum(cnt.values())
    print(f"failed tasks: {total}")
    for name, _ in PATTERNS:
        print(f"  {name:<32}{cnt[name]:>5}")
    print(f"  {'Unclassified':<32}{cnt['Unclassified']:>5}")
    assert total == len(fails)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "paper/data/tasks.json")
