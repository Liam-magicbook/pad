#!/usr/bin/env bash
# PAD 一键起步：装依赖 → 起服务端 → 起一台本地节点 → 打一个真任务，看到闭环。
#
#   ./quickstart.sh                 # 默认端口 8790，数据落 ./data
#   PORT=9000 ./quickstart.sh       # 换端口
#   ./quickstart.sh --stop          # 停掉这次起的进程
#
# 做完你会有：
#   http://127.0.0.1:8790/pad/<KEY>/mcp   ← 填进 ChatGPT 的 MCP Server URL
#   ./data/quickstart.log                 ← 服务端与节点的输出
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
PORT="${PORT:-8790}"
DATA="${ROOT}/data"
KEY_FILE="${DATA}/.pad_key"
PID_FILE="${DATA}/pids"

say() { printf '\033[1;36m▸\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✗\033[0m %s\n' "$*" >&2; exit 1; }

stop_all() {
  [[ -f "$PID_FILE" ]] || { echo "没有正在跑的 PAD（${PID_FILE} 不存在）"; return 0; }
  while read -r pid; do
    kill "$pid" 2>/dev/null && echo "已停 PID $pid" || true
  done < "${PID_FILE}"
  rm -f "${PID_FILE}"
}

if [[ "${1:-}" == "--stop" ]]; then stop_all; exit 0; fi

# ---------- 0. 环境 ----------
command -v python3 >/dev/null || die "需要 python3（3.10+）"
PYV="$(python3 -c 'import sys; print("%d%d" % sys.version_info[:2])')"
[[ "${PYV}" -ge 310 ]] || die "python3 版本太老：${PYV}，需要 3.10+"

mkdir -p "$DATA/logs"
LOG="${DATA}/quickstart.log"
: > "${LOG}"

# ---------- 1. 虚拟环境 + 依赖 ----------
if [[ ! -x "${ROOT}/.venv/bin/python" ]]; then
  say "建虚拟环境（.venv）"
  python3 -m venv "${ROOT}/.venv"
fi
say "装依赖（requirements.txt）"
"${ROOT}/.venv/bin/pip" install -q --upgrade pip >>"$LOG" 2>&1
"${ROOT}/.venv/bin/pip" install -q -r "${ROOT}/requirements.txt" >>"${LOG}" 2>&1 \
  || die "依赖装失败，看 ${LOG}"

# ---------- 2. 密钥 ----------
if [[ ! -f "${KEY_FILE}" ]]; then
  say "生成访问密钥（${KEY_FILE}，权限 600）"
  "${ROOT}/.venv/bin/python" - <<'PY' > "${KEY_FILE}"
import secrets
print(secrets.token_urlsafe(24))
PY
  chmod 600 "${KEY_FILE}"
fi
KEY="$(cat "${KEY_FILE}")"
BASE="http://127.0.0.1:${PORT}/pad/${KEY}"

# ---------- 3. 起服务端 ----------
if lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
  die "端口 ${PORT} 已被占用。换一个：PORT=9000 ./quickstart.sh"
fi

say "起 PAD 服务端 :${PORT}（数据在 ${DATA}）"
PAD_KEY="${KEY}" \
PAD_DB="${DATA}/pad.db" \
PAD_ARTIFACT_ROOT="${DATA}/artifacts" \
PAD_INPUT_FILE_ROOT="${DATA}/input_files" \
PAD_ALLOWED_HOSTS="localhost,127.0.0.1" \
nohup "${ROOT}/.venv/bin/python" -m uvicorn server:app \
  --host 127.0.0.1 --port "$PORT" --log-level info >>"${LOG}" 2>&1 &
echo $! >> "$PID_FILE"

for _ in $(seq 1 60); do
  curl -fsS "${BASE}/api/digest" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -fsS "${BASE}/api/digest" >/dev/null 2>&1 || die "服务端没起来，看 ${LOG}"

# ---------- 4. 起一台本地节点 ----------
say "起本地节点 local-1（能力 demo.echo / local.run）"
cat > "${DATA}/ld_config.yaml" <<YAML
node_id: local-1
role: compute
description: "本机演示节点：跑 demo.echo（回 pong）和 local.run（跑一条命令）。"
workspace: $DATA/workspace

capabilities:
  demo.echo:
    desc: "自检：回一句 pong，证明注册/领活/报告三段都通"
    cmd: "${ROOT}/.venv/bin/python ${ROOT}/harness/run_script.py 'echo \\"pong from \$(hostname -s) at \$(date +%H:%M)\\"'"
    timeout_min: 1

  local.run:
    desc: "在跑 PAD 的这台机器上执行一条命令，并把 stdout 交回上层"
    cmd: "${ROOT}/.venv/bin/python ${ROOT}/harness/run_script.py 'echo hello from \$(hostname -s)'"
    timeout_min: 10

pad_url: "$BASE"
poll_sec: 3
work_dir: "$DATA/work"
YAML

mkdir -p "${DATA}/workspace" "${DATA}/work"
PAD_KEY="${KEY}" nohup "${ROOT}/.venv/bin/python" -u "${ROOT}/ld_agent.py" \
  "${DATA}/ld_config.yaml" >>"${LOG}" 2>&1 &
echo $! >> "$PID_FILE"

for _ in $(seq 1 40); do
  curl -fsS "${BASE}/api/nodes" | grep -q local-1 && break
  sleep 0.5
done
curl -fsS "${BASE}/api/nodes" | grep -q local-1 || die "节点没注册上，看 ${LOG}"

# ---------- 5. 打一个真任务 ----------
say "下发任务 demo.echo，等节点领活并回报"
PAD_URL="${BASE}" "${ROOT}/padctl.py" submit \
  --capability demo.echo --goal "自检：回一句 pong" --acceptance "返回 pong" >/dev/null

TASK=""
for _ in $(seq 1 60); do
  TASK="$(curl -fsS "${BASE}/api/digest" \
    | "${ROOT}/.venv/bin/python" -c 'import json,sys; d=json.load(sys.stdin); print((d["nodes"][0]["recent_tasks"] or [{}])[0].get("task_id",""))')"
  [[ -n "$TASK" ]] && break
  sleep 0.5
done
[[ -n "$TASK" ]] || die "没拿到任务 id，看 ${LOG}"

sleep 3
PAD_URL="${BASE}" "${ROOT}/padctl.py" task "${TASK}" | head -40

cat <<EOF

────────────────────────────────────────────
PAD 已经跑起来了。

  服务端      $BASE
  MCP 地址    $BASE/mcp        ← 填进 ChatGPT / Claude 的 MCP 配置
  密钥        $KEY
  日志        $LOG
  数据        $DATA

下一步：
  1) 把这个 MCP 地址接进 ChatGPT：
     Settings → Connectors(Developer mode) → Create → 填 URL → Scan tools
  2) 加一台真机器：cp $DATA/ld_config.yaml 改 node_id/capabilities
     或直接看 README 的「加一台机器」
  3) 停掉：./quickstart.sh --stop

注意：密钥就在 URL 路径里，别把带密钥的地址贴到公开场合。
────────────────────────────────────────────
EOF
