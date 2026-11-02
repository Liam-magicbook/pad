#!/usr/bin/env bash
# End-to-end drill: deliver files with a task -> node downloads and verifies -> executor sees
# the files -> results come back
#
# Uses only a local temp database and a temp work_dir; never touches the live PAD or a real
# node's work directory.
set -euo pipefail

PAD_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d "${TMPDIR:-/tmp}/pad-input-e2e.XXXXXX")"
PORT="${PORT:-8799}"
export PAD_KEY="e2etestkey"
export PAD_DB="$TMP/pad.db"
export PAD_INPUT_FILE_ROOT="$TMP/input_files"
export PAD_ARTIFACT_ROOT="$TMP/artifacts"
BASE="http://127.0.0.1:$PORT/pad/$PAD_KEY"

cleanup() {
  [[ -n "${SERVER_PID:-}" ]] && kill "$SERVER_PID" 2>/dev/null || true
  [[ -n "${LD_PID:-}" ]] && kill "$LD_PID" 2>/dev/null || true
  wait 2>/dev/null || true
}
trap cleanup EXIT

echo "== 临时目录 $TMP"

cd "$PAD_DIR"
.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port "$PORT" --log-level warning \
  > "$TMP/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do
  curl -fsS "$BASE/api/nodes" >/dev/null 2>&1 && break
  sleep 0.25
done

# Node config: the capability command simply "lists the files in inputs/ and writes result.json"
cat > "$TMP/ld_config.yaml" <<YAML
node_id: e2e-node
role: test
description: "端到端演练节点"
workspace: $TMP
work_dir: $TMP/work
poll_sec: 1
pad_url: $BASE
capabilities:
  test.echo:
    desc: "把收到的输入文件读一遍并回写结果"
    timeout_min: 3
    cmd: "\$PAD_HARNESS/echo_cmd.sh"
YAML

cat > "$TMP/echo_cmd.sh" <<'SH'
set -eu
cd "$PAD_WORK_DIR"
{
  echo "goal=$PAD_GOAL"
  echo "--- inputs ---"
  ls -l inputs/
  echo "--- prompt section ---"
  grep -A4 "随任务下发的文件" prompt.md || true
  echo "--- content ---"
  for f in inputs/*; do
    [ -f "$f" ] || continue
    echo "[$f]"
    head -c 200 "$f"; echo
  done
} > run.log 2>&1
python3 - <<'PY'
import json, os, pathlib
files = json.loads(os.environ["PAD_INPUT_FILES"])
pathlib.Path("result.json").write_text(json.dumps({
    "status": "done",
    "summary": f"收到 {len(files)} 个文件",
    "report": "端到端演练：文件已落盘并读取。",
    "acceptance_results": [{"item": "文件到达 inputs/", "passed": bool(files), "evidence": str(files)}],
    "seen": [{"name": f["name"], "bytes": f["bytes"], "mime_type": f["mime_type"]} for f in files],
}, ensure_ascii=False))
PY
SH
chmod +x "$TMP/echo_cmd.sh"
export PAD_HARNESS="$TMP"

.venv/bin/python ld_agent.py "$TMP/ld_config.yaml" > "$TMP/ld.log" 2>&1 &
LD_PID=$!
for _ in $(seq 1 40); do
  grep -q "registered e2e-node" "$TMP/ld.log" 2>/dev/null && break
  sleep 0.25
done

# Create three real byte payloads: a Chinese-named PDF, a CSV, a PNG
.venv/bin/python - "$TMP/payload.json" <<'PY'
import base64, json, sys
png = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
files = [
    {"name": "季度报告.pdf", "mime_type": "application/pdf",
     "data_base64": base64.b64encode(b"%PDF-1.4\nquarterly report body\n%%EOF\n").decode()},
    {"name": "scores.csv", "mime_type": "text/csv",
     "data_base64": base64.b64encode(b"name,score\nli,91\n").decode()},
    {"name": "preview.png", "mime_type": "image/png",
     "data_base64": base64.b64encode(png).decode()},
]
json.dump({"capability": "test.echo", "goal": "读一遍随任务下发的文件",
           "acceptance": ["文件到达 inputs/"], "input_files": files,
           "created_by": "human"}, open(sys.argv[1], "w"), ensure_ascii=False)
PY

TASK_ID=$(curl -fsS -X POST "$BASE/api/task" -H 'Content-Type: application/json' \
  --data-binary @"$TMP/payload.json" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["task_id"])')
echo "== 下发任务 $TASK_ID"

for _ in $(seq 1 120); do
  STATUS=$(curl -fsS "$BASE/api/task/$TASK_ID" | .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["status"])')
  [[ "$STATUS" == "done" || "$STATUS" == "failed" ]] && break
  sleep 0.5
done

echo "== 任务状态 $STATUS"
curl -fsS "$BASE/api/task/$TASK_ID" | .venv/bin/python -c '
import json, sys
t = json.load(sys.stdin)
print("input_files:", [(x["name"], x["mime_type"], x["bytes"]) for x in t["input_files"]])
for r in t["reports"]:
    if r["level"] in ("done", "failed"):
        print("report:", r["summary"])
        print("seen:", r["payload"].get("seen"))
'
echo "== 节点落地情况"
ls -l "$TMP/work/$TASK_ID/inputs/"
echo "== 下载校验（中文名走 RFC 5987）"
curl -fsS -D "$TMP/dl.headers" "$BASE/api/task/$TASK_ID/input-file?name=%E5%AD%A3%E5%BA%A6%E6%8A%A5%E5%91%8A.pdf" \
  -o "$TMP/dl.pdf"
grep -i "content-disposition\|x-pad-sha256" "$TMP/dl.headers"
head -1 "$TMP/dl.pdf"

[[ "$STATUS" == "done" ]] || { echo "!! 任务未完成，日志："; tail -40 "$TMP/ld.log" "$TMP/server.log"; exit 1; }
echo "== 端到端演练通过：${TMP}"
