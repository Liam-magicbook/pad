import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("PAD_DB", Path(__file__).parent / "pad.db"))
CST = timezone(timedelta(hours=8))
PRIORITY_ORDER = {"high": 0, "normal": 1, "low": 2}

# Artifact storage (the upper layer keeps a copy): per-file cap, per-task cap, retention days
ARTIFACT_ROOT = Path(os.environ.get("PAD_ARTIFACT_ROOT", Path(__file__).parent / "artifacts"))
ARTIFACT_MAX_BYTES = int(os.environ.get("PAD_ARTIFACT_MAX_BYTES", 5 * 1024 * 1024))
ARTIFACT_MAX_TASK_BYTES = int(os.environ.get("PAD_ARTIFACT_MAX_TASK_BYTES", 20 * 1024 * 1024))
ARTIFACT_TTL_DAYS = int(os.environ.get("PAD_ARTIFACT_TTL_DAYS", 30))
# Input files shipped with a task. Early on only images were accepted; now they're generic
# files: images stay images (native ImageContent), PDF/CSV/zip/mp4 etc. land on disk as-is.
INPUT_FILE_ROOT = Path(os.environ.get("PAD_INPUT_FILE_ROOT")
                       or os.environ.get("PAD_INPUT_IMAGE_ROOT")
                       or Path(__file__).parent / "input_files")
INPUT_FILE_MAX_BYTES = int(os.environ.get("PAD_INPUT_FILE_MAX_BYTES", 10 * 1024 * 1024))
INPUT_FILE_MAX_TOTAL_BYTES = int(os.environ.get("PAD_INPUT_FILE_MAX_TOTAL_BYTES", 15 * 1024 * 1024))
INPUT_FILE_MAX_COUNT = int(os.environ.get("PAD_INPUT_FILE_MAX_COUNT", 8))
# Input file retention: keep them this long after the task is terminal, so the disk survives.
INPUT_FILE_TTL_DAYS = int(os.environ.get("PAD_INPUT_FILE_TTL_DAYS", 14))

# Old names kept: the server or external scripts may still reference the caps in "image" terms.
INPUT_IMAGE_ROOT = INPUT_FILE_ROOT
INPUT_IMAGE_MAX_BYTES = INPUT_FILE_MAX_BYTES
INPUT_IMAGE_MAX_COUNT = INPUT_FILE_MAX_COUNT


def now() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{datetime.now(CST).strftime('%Y%m%d')}-{uuid.uuid4().hex[:6]}"


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=15, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=15000")
    return c


def init() -> None:
    with conn() as c:
        c.executescript(Path(__file__).with_name("schema.sql").read_text())
        cols = {r["name"] for r in c.execute("PRAGMA table_info(nodes)").fetchall()}
        if "description" not in cols:
            c.execute("ALTER TABLE nodes ADD COLUMN description TEXT")
        # client_id idempotency key: the server records a report once even if a node re-sends it
        cols = {r["name"] for r in c.execute("PRAGMA table_info(reports)").fetchall()}
        if "client_id" not in cols:
            c.execute("ALTER TABLE reports ADD COLUMN client_id TEXT")
        c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_cid ON reports(client_id)")
        # Collapse legacy pending tasks into failed with a visible report, so nothing stays blocked.
        ts = now()
        c.execute("""INSERT INTO reports(task_id, node_id, level, summary, payload, created_at)
                     SELECT t.task_id, t.node_id, 'failed',
                            '旧版待决任务已停止，需要按原 goal 重新提交',
                            '{"report":"该任务来自旧版人工拍板流程，升级已移除，未自动重跑以避免重复副作用。请检查已有产物后按原 goal 重新提交。"}',
                            ?
                     FROM tasks t WHERE t.status='blocked'
                       AND NOT EXISTS (SELECT 1 FROM reports r WHERE r.task_id=t.task_id
                                       AND r.level IN ('done','failed'))""", (ts,))
        c.execute("UPDATE tasks SET status='failed', updated_at=? WHERE status='blocked'", (ts,))


def _j(v, default):
    if v is None:
        return default
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return default


# ---------- nodes ----------

def register_node(node_id, role, capabilities, meta=None, description="") -> dict:
    with conn() as c:
        c.execute(
            """INSERT INTO nodes(node_id, role, description, capabilities, meta, last_seen)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(node_id) DO UPDATE SET
                 role=excluded.role,
                 description=excluded.description,
                 capabilities=excluded.capabilities,
                 meta=excluded.meta,
                 last_seen=excluded.last_seen""",
            (node_id, role, description, json.dumps(capabilities, ensure_ascii=False),
             json.dumps(meta or {}, ensure_ascii=False), now()),
        )
    return {"ok": True, "node_id": node_id, "capabilities": capabilities}


def heartbeat(node_id, summary) -> dict:
    with conn() as c:
        c.execute("UPDATE nodes SET last_seen=?, summary=? WHERE node_id=?",
                  (now(), summary, node_id))
    return {"ok": True}


def list_nodes() -> list:
    with conn() as c:
        rows = c.execute("SELECT * FROM nodes ORDER BY last_seen DESC").fetchall()
    return [{
        "node_id": r["node_id"], "role": r["role"], "description": r["description"],
        "capabilities": _j(r["capabilities"], []),
        "capabilities_detail": _j(r["meta"], {}).get("capabilities_detail", {}),
        "bundle_hash": _j(r["meta"], {}).get("bundle_hash", ""),
        "summary": r["summary"], "last_seen": r["last_seen"],
    } for r in rows]


# ---------- tasks ----------

def submit_task(capability, goal, constraints=None, acceptance=None,
                deadline=None, priority="normal", created_by="chatgpt",
                input_images=None, input_files=None) -> dict:
    """Create a task. input_files / input_images are two names for the same thing (older callers).

    Each entry looks like {"name", "mime_type", "data"}, where data is already-decoded bytes.
    """
    task_id = new_id("t")
    ts = now()
    files = list(input_files if input_files is not None else (input_images or []))
    if len(files) > INPUT_FILE_MAX_COUNT:
        raise ValueError(f"每个任务最多接收 {INPUT_FILE_MAX_COUNT} 个输入文件")
    total_bytes = sum(len(x["data"]) for x in files)
    for f in files:
        if len(f["data"]) > INPUT_FILE_MAX_BYTES:
            raise ValueError(f"输入文件 {f.get('name')} 超过 {INPUT_FILE_MAX_BYTES} 字节上限")
    if total_bytes > INPUT_FILE_MAX_TOTAL_BYTES:
        raise ValueError(f"输入文件合计不能超过 {INPUT_FILE_MAX_TOTAL_BYTES} 字节")

    file_root = INPUT_FILE_ROOT / task_id
    rows = []
    try:
        for f in files:
            name = _safe_input_name(f["name"])
            data = bytes(f["data"])
            stored = file_root / name
            stored.parent.mkdir(parents=True, exist_ok=True)
            tmp = stored.with_suffix(stored.suffix + ".tmp")
            tmp.write_bytes(data)
            tmp.replace(stored)
            rows.append((task_id, name, f["mime_type"], len(data),
                         hashlib.sha256(data).hexdigest(), str(stored), ts))

        c = conn()
        try:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                """INSERT INTO tasks(task_id, capability, goal, constraints, acceptance,
                                     deadline, priority, status, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,'queued',?,?,?)""",
                (task_id, capability, goal,
                 json.dumps(constraints or [], ensure_ascii=False),
                 json.dumps(acceptance or [], ensure_ascii=False),
                 deadline, priority, created_by, ts, ts),
            )
            if rows:
                c.executemany(
                    """INSERT INTO task_inputs(task_id,name,mime_type,bytes,sha256,stored_path,created_at)
                       VALUES(?,?,?,?,?,?,?)""", rows)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        finally:
            c.close()
    except Exception:
        shutil.rmtree(file_root, ignore_errors=True)
        raise
    listed = [{"name": r[1], "mime_type": r[2], "bytes": r[3], "sha256": r[4]} for r in rows]
    return {"ok": True, "task_id": task_id, "capability": capability, "status": "queued",
            "input_files": listed, "input_images": listed}


def _safe_input_name(name: str) -> str:
    """On-disk filename: strip path, control chars and filesystem-hostile symbols, keep Chinese.

    Last line of defence (the server already sanitized once): only guarantees "no writes outside
    the directory, no illegal filename", no ASCII downgrade — the executor must recognise the file.
    """
    name = str(name).replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable() and c not in '/\\:*?"<>|')
    name = name.strip(" .")
    if not name:
        raise ValueError("输入文件必须有有效文件名")
    return name[:160]


def claim_task(node_id, capabilities, server_bundle: str = "") -> dict | None:
    caps = list(capabilities or [])
    if not caps:
        return None
    marks = ",".join("?" for _ in caps)
    with conn() as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            node = c.execute("SELECT meta FROM nodes WHERE node_id=?", (node_id,)).fetchone()
            node_bundle = _j(node["meta"], {}).get("bundle_hash", "") if node else ""
            supports_input_files = bool(server_bundle and node_bundle == server_bundle)
            # An old skeleton (LD that only downloads images) may still claim image-only tasks
            # under the old caps (4 files / 5MiB); anything bigger fails the whole task.
            row = c.execute(
                f"""SELECT * FROM tasks
                    WHERE status='queued' AND capability IN ({marks})
                      AND (?=1
                           OR NOT EXISTS (
                                SELECT 1 FROM task_inputs i WHERE i.task_id=tasks.task_id
                              )
                           OR (
                                (SELECT COUNT(*) FROM task_inputs i
                                   WHERE i.task_id=tasks.task_id) <= 4
                                AND NOT EXISTS (
                                  SELECT 1 FROM task_inputs i WHERE i.task_id=tasks.task_id
                                    AND (i.bytes > 5242880 OR i.mime_type NOT LIKE 'image/%')
                                )
                              ))
                    ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END,
                             created_at ASC
                    LIMIT 1""",
                (*caps, 1 if supports_input_files else 0),
            ).fetchone()
            if row is None:
                c.execute("COMMIT")
                return None
            c.execute("UPDATE tasks SET status='claimed', node_id=?, updated_at=? WHERE task_id=?",
                      (node_id, now(), row["task_id"]))
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return task_view(row["task_id"])


def task_view(task_id) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if r is None:
            return None
        reports = c.execute(
            "SELECT * FROM reports WHERE task_id=? ORDER BY report_id ASC LIMIT 50",
            (task_id,)).fetchall()
        arts = c.execute(
            "SELECT * FROM artifacts WHERE task_id=? ORDER BY artifact_id ASC",
            (task_id,)).fetchall()
        inputs = c.execute(
            "SELECT name,mime_type,bytes,sha256,created_at FROM task_inputs "
            "WHERE task_id=? ORDER BY input_id ASC", (task_id,)).fetchall()
    return {
        "task_id": r["task_id"], "capability": r["capability"], "goal": r["goal"],
        "constraints": _j(r["constraints"], []), "acceptance": _j(r["acceptance"], []),
        "deadline": r["deadline"], "priority": r["priority"], "status": r["status"],
        "node_id": r["node_id"], "created_by": r["created_by"],
        "created_at": r["created_at"], "updated_at": r["updated_at"],
        "reports": [{"level": x["level"], "summary": x["summary"], "client_id": x["client_id"],
                     "payload": _j(x["payload"], {}), "at": x["created_at"]} for x in reports],
        "artifacts": [{                       # kept upstream, fetchable piecewise by name
            "name": x["name"], "bytes": x["bytes"], "sha256": x["sha256"],
            "rows": x["rows"], "schema": x["schema"], "node_id": x["node_id"],
            "local_path": x["local_path"], "uploaded_at": x["uploaded_at"],
        } for x in arts],
        "input_files": [dict(x) for x in inputs],
        "input_images": [dict(x) for x in inputs],
    }


def read_task_input_file(task_id: str, name: str) -> dict:
    with conn() as c:
        r = c.execute("SELECT * FROM task_inputs WHERE task_id=? AND name=?",
                      (task_id, str(name))).fetchone()
    if r is None:
        view = task_view(task_id)
        names = [x["name"] for x in view["input_files"]] if view else []
        return {"error": "not_found", "name": name, "available": names}
    path = Path(r["stored_path"])
    if not path.is_file():
        return {"error": "file_missing", "name": name}
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != r["bytes"] or digest != r["sha256"]:
        return {"error": "integrity_mismatch", "name": name}
    return {"meta": {"name": r["name"], "mime_type": r["mime_type"],
                      "bytes": r["bytes"], "sha256": r["sha256"]}, "data": data}


# Old name: nodes/scripts were written against the input-image endpoint, so keep a usable alias.
read_task_input_image = read_task_input_file


def task_status(task_id) -> dict | None:
    """Lightweight status query: nodes poll it mid-run for "was my work cancelled"; no full view."""
    with conn() as c:
        r = c.execute("SELECT task_id, status, updated_at FROM tasks WHERE task_id=?",
                      (task_id,)).fetchone()
    return dict(r) if r else None


def node_recent_tasks(node_id, limit=10) -> list:
    """A machine's recent tasks (updated_at desc), id + status only — no full task_view."""
    with conn() as c:
        rows = c.execute(
            "SELECT task_id, capability, status, updated_at FROM tasks "
            "WHERE node_id=? ORDER BY updated_at DESC, task_id DESC LIMIT ?",
            (node_id, max(1, int(limit)))).fetchall()
    return [dict(r) for r in rows]


def node_tasks(node_id, statuses) -> list:
    marks = ",".join("?" for _ in statuses)
    with conn() as c:
        rows = c.execute(
            f"""SELECT task_id FROM tasks WHERE node_id=? AND status IN ({marks})
                ORDER BY updated_at ASC""", (node_id, *statuses)).fetchall()
    return [task_view(r["task_id"]) for r in rows]


def _coverage(items, results) -> dict | None:
    """Per-item acceptance coverage (only judges "was it answered", not truth — that needs a node).

    Without this, a report can bury an acceptance item under one summary and nobody notices.
    """
    if not items:
        return None
    if not isinstance(results, list) or not results:
        return {"total": len(items), "covered": 0,
                "missing": list(range(1, len(items) + 1)),
                "note": "报告里没有 acceptance_results，无法逐条核对"}
    labels = {str(e.get("item", "")).strip() for e in results if isinstance(e, dict)}
    by_position = len(results) >= len(items) and not (labels - {""})
    covered, missing = 0, []
    for i, item in enumerate(items, 1):
        if by_position or str(i) in labels or str(item).strip() in labels:
            covered += 1
        else:
            missing.append(i)
    out = {"total": len(items), "covered": covered, "missing": missing}
    if missing:
        out["note"] = f"第 {missing} 条验收在报告里没有对应的回答"
    return out


def report(task_id, node_id, level, summary, payload=None, client_id=None) -> dict:
    payload = dict(payload or {})
    with conn() as c:
        if client_id:
            row = c.execute("SELECT report_id FROM reports WHERE client_id=?",
                            (client_id,)).fetchone()
            if row:
                return {"ok": True, "duplicate": True}      # re-send, record only once
        if level in ("done", "failed"):                     # terminal states carry coverage
            t = c.execute("SELECT acceptance FROM tasks WHERE task_id=?", (task_id,)).fetchone()
            check = _coverage(_j(t["acceptance"], []) if t else [], payload.get("acceptance_results"))
            if check:
                payload["_acceptance_check"] = check
        c.execute(
            "INSERT INTO reports(task_id, node_id, level, summary, payload, client_id, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (task_id, node_id, level, summary,
             json.dumps(payload, ensure_ascii=False), client_id, now()),
        )
        new_status = {"progress": "running", "done": "done", "failed": "failed"}.get(level)
        if new_status:
            # progress must not drag a terminal task back to running (late reports do that)
            c.execute("""UPDATE tasks SET status=?, updated_at=?
                         WHERE task_id=? AND status NOT IN ('done','failed','cancelled')""",
                      (new_status, now(), task_id))
    return {"ok": True, "acceptance_check": payload.get("_acceptance_check")}


# ---------- digest ----------

def digest(server_bundle: str = "", recent_limit: int = 10) -> dict:
    nodes = list_nodes()
    for n in nodes:                              # each machine's recent task ids (newest first)
        n["recent_tasks"] = node_recent_tasks(n["node_id"], recent_limit)
        # Matching skeletons = this LD can already download input files (old ones: images only).
        n["input_files_supported"] = bool(server_bundle and n.get("bundle_hash") == server_bundle)
        n["input_images_supported"] = n["input_files_supported"]
    with conn() as c:
        running = c.execute(
            "SELECT task_id, capability, goal, status, node_id, updated_at FROM tasks "
            "WHERE status IN ('queued','claimed','running') ORDER BY updated_at DESC LIMIT 30").fetchall()
        done_today = c.execute(
            "SELECT COUNT(*) n FROM tasks WHERE status='done' AND date(updated_at)=date(?)",
            (now(),)).fetchone()["n"]
    stale = [n for n in nodes
             if not n["last_seen"] or (datetime.fromisoformat(now()) - datetime.fromisoformat(n["last_seen"])) > timedelta(minutes=30)]
    stale_bundle = [n["node_id"] for n in nodes
                    if server_bundle and n.get("bundle_hash") != server_bundle]
    return {
        "now": now(),
        "server_bundle": server_bundle,
        "nodes_bundle_stale": stale_bundle,
        "nodes": nodes,
        "nodes_stale": [n["node_id"] for n in stale],
        "active_tasks": [dict(r) for r in running],
        "done_today": done_today,
    }


# ---------- artifacts (kept upstream) ----------------------------------------

def _safe_name(name: str) -> str:
    parts = [p for p in str(name).replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return "__".join(parts)[:180] or "artifact"


def save_artifact(task_id, node_id, name, data: bytes, rows="", schema="", local_path="") -> dict:
    name = str(name)
    if len(data) > ARTIFACT_MAX_BYTES:
        raise ValueError(f"单文件超过 {ARTIFACT_MAX_BYTES} 字节上限，请留在本地并只回元数据")
    with conn() as c:
        used = c.execute("SELECT COALESCE(SUM(bytes),0) n FROM artifacts WHERE task_id=?",
                         (task_id,)).fetchone()["n"]
    if used + len(data) > ARTIFACT_MAX_TASK_BYTES:
        raise ValueError(f"本任务产物累计将超过 {ARTIFACT_MAX_TASK_BYTES} 字节上限")
    root = ARTIFACT_ROOT / str(task_id)
    root.mkdir(parents=True, exist_ok=True)
    stored = root / _safe_name(name)
    stored.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    with conn() as c:
        c.execute(
            """INSERT INTO artifacts(task_id, node_id, name, bytes, sha256, rows, schema,
                                     local_path, stored_path, uploaded_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(task_id, name) DO UPDATE SET
                 bytes=excluded.bytes, sha256=excluded.sha256, rows=excluded.rows,
                 schema=excluded.schema, local_path=excluded.local_path,
                 stored_path=excluded.stored_path, uploaded_at=excluded.uploaded_at""",
            (task_id, node_id, name, len(data), digest, str(rows), str(schema),
             local_path, str(stored), now()))
    return {"ok": True, "task_id": task_id, "name": name, "bytes": len(data), "sha256": digest}


def read_artifact(task_id, name, offset: int = 0, limit: int = 8000) -> dict:
    """Fetch one chunk of an artifact (8KB default), page with offset. Upper layer pulls slices."""
    with conn() as c:
        r = c.execute("SELECT * FROM artifacts WHERE task_id=? AND name=?",
                      (task_id, str(name))).fetchone()
    if r is None:
        names = [a["name"] for a in list_artifacts(task_id)]
        return {"error": "not_found", "name": name, "available": names}
    p = Path(r["stored_path"])
    if not p.exists():
        return {"error": "file_missing", "name": name}
    data = p.read_bytes()
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 200000))
    chunk = data[offset:offset + limit]
    return {"name": r["name"], "bytes": r["bytes"], "sha256": r["sha256"],
            "rows": r["rows"], "schema": r["schema"], "local_path": r["local_path"],
            "offset": offset, "returned": len(chunk),
            "next_offset": offset + len(chunk) if offset + len(chunk) < len(data) else None,
            "content": chunk.decode("utf-8", errors="replace")}


def read_artifact_bytes(task_id, name) -> dict:
    """Raw artifact bytes + metadata, so MCP can branch on MIME (images → ImageContent, else pages).

    Returns {"meta": {...}, "data": bytes}; {"error": ...} when not found or the file is missing.
    """
    with conn() as c:
        r = c.execute("SELECT * FROM artifacts WHERE task_id=? AND name=?",
                      (task_id, str(name))).fetchone()
    if r is None:
        names = [a["name"] for a in list_artifacts(task_id)]
        return {"error": "not_found", "name": name, "available": names}
    p = Path(r["stored_path"])
    if not p.exists():
        return {"error": "file_missing", "name": name}
    meta = {"name": r["name"], "bytes": r["bytes"], "sha256": r["sha256"],
            "rows": r["rows"], "schema": r["schema"], "local_path": r["local_path"]}
    return {"meta": meta, "data": p.read_bytes()}


def list_artifacts(task_id) -> list:
    with conn() as c:
        rows = c.execute("SELECT * FROM artifacts WHERE task_id=? ORDER BY artifact_id ASC",
                         (task_id,)).fetchall()
    return [{"name": r["name"], "bytes": r["bytes"], "sha256": r["sha256"],
             "rows": r["rows"], "schema": r["schema"], "node_id": r["node_id"],
             "local_path": r["local_path"], "uploaded_at": r["uploaded_at"]} for r in rows]


def sweep_input_files(ttl_days: int = INPUT_FILE_TTL_DAYS) -> dict:
    """Input files expire too: nobody re-reads them after the task, they only fill the disk.

    The criterion is "task already terminal and last updated before cutoff", not upload
    time — a task that ran for two days must not lose its inputs before it finishes.
    """
    cutoff = (datetime.now(CST) - timedelta(days=ttl_days)).isoformat(timespec="seconds")
    with conn() as c:
        rows = c.execute(
            """SELECT i.input_id, i.task_id, i.stored_path FROM task_inputs i
               JOIN tasks t ON t.task_id = i.task_id
               WHERE t.status IN ('done','failed','cancelled') AND t.updated_at < ?""",
            (cutoff,)).fetchall()
        for r in rows:
            try:
                Path(r["stored_path"]).unlink(missing_ok=True)
            except OSError:
                pass
            c.execute("DELETE FROM task_inputs WHERE input_id=?", (r["input_id"],))
        left = c.execute("SELECT COUNT(*) n FROM task_inputs").fetchone()["n"]
    if INPUT_FILE_ROOT.exists():
        for d in INPUT_FILE_ROOT.iterdir():        # emptied task directories
            if d.is_dir() and not any(d.iterdir()):
                shutil.rmtree(d, ignore_errors=True)
    return {"deleted": len(rows), "kept": left, "ttl_days": ttl_days, "cutoff": cutoff}


def sweep_artifacts(ttl_days: int = ARTIFACT_TTL_DAYS) -> dict:
    """Expiry sweep, so the server doesn't quietly fill up."""
    cutoff = (datetime.now(CST) - timedelta(days=ttl_days)).isoformat(timespec="seconds")
    with conn() as c:
        old = c.execute("SELECT artifact_id, stored_path FROM artifacts WHERE uploaded_at < ?",
                        (cutoff,)).fetchall()
        for r in old:
            try:
                Path(r["stored_path"]).unlink(missing_ok=True)
            except OSError:
                pass
            c.execute("DELETE FROM artifacts WHERE artifact_id=?", (r["artifact_id"],))
        left = c.execute("SELECT COUNT(*) n FROM artifacts").fetchone()["n"]
    if ARTIFACT_ROOT.exists():
        for d in ARTIFACT_ROOT.iterdir():          # emptied task directories
            if d.is_dir() and not any(d.iterdir()):
                shutil.rmtree(d, ignore_errors=True)
    return {"deleted": len(old), "kept": left, "ttl_days": ttl_days, "cutoff": cutoff}


def forget_node(node_id) -> dict:
    """Drop a machine from the node list when it is retired or left over from testing."""
    with conn() as c:
        cur = c.execute("DELETE FROM nodes WHERE node_id=?", (node_id,))
    return {"ok": True, "node_id": node_id, "deleted": cur.rowcount}


def close_task(task_id, reason="", by="chatgpt") -> dict:
    """Upper layer cancels the task; the node sees cancelled and kills its local process group."""
    with conn() as c:
        r = c.execute("SELECT status FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if r is None:
            return {"ok": False, "error": "not_found"}
        c.execute("UPDATE tasks SET status='cancelled', updated_at=? WHERE task_id=?",
                  (now(), task_id))
    return {"ok": True, "task_id": task_id, "status": "cancelled", "reason": reason}
