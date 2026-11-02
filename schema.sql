CREATE TABLE IF NOT EXISTS nodes (
  node_id      TEXT PRIMARY KEY,
  role         TEXT,
  description  TEXT,
  capabilities TEXT NOT NULL DEFAULT '[]',
  summary      TEXT,
  meta         TEXT DEFAULT '{}',
  last_seen    TEXT
);

CREATE TABLE IF NOT EXISTS tasks (
  task_id     TEXT PRIMARY KEY,
  capability  TEXT NOT NULL,
  goal        TEXT NOT NULL,
  constraints TEXT DEFAULT '[]',
  acceptance  TEXT DEFAULT '[]',
  deadline    TEXT,
  priority    TEXT DEFAULT 'normal',
  status      TEXT DEFAULT 'queued',
  node_id     TEXT,
  created_by  TEXT,
  created_at  TEXT,
  updated_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(capability, status, priority);

CREATE TABLE IF NOT EXISTS reports (
  report_id  INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id    TEXT,
  node_id    TEXT,
  level      TEXT NOT NULL,
  summary    TEXT,
  payload    TEXT DEFAULT '{}',
  client_id  TEXT,
  created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_reports_task ON reports(task_id);

-- Artifacts stored on the upper layer (small files upload automatically; large files stay on
-- the node with metadata only)
CREATE TABLE IF NOT EXISTS artifacts (
  artifact_id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT,
  node_id     TEXT,
  name        TEXT NOT NULL,
  bytes       INTEGER,
  sha256      TEXT,
  rows        TEXT,
  schema      TEXT,
  local_path  TEXT,
  stored_path TEXT,
  uploaded_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_artifacts_task_name ON artifacts(task_id, name);

-- Input files the user sends along with a task over MCP (images and ordinary files share this
-- table), kept separate from the artifacts nodes send back.
CREATE TABLE IF NOT EXISTS task_inputs (
  input_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id     TEXT NOT NULL,
  name        TEXT NOT NULL,
  mime_type   TEXT NOT NULL,
  bytes       INTEGER NOT NULL,
  sha256      TEXT NOT NULL,
  stored_path TEXT NOT NULL,
  created_at  TEXT,
  UNIQUE(task_id, name)
);
CREATE INDEX IF NOT EXISTS idx_task_inputs_task ON task_inputs(task_id);
