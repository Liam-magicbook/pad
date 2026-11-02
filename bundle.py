"""Version fingerprint of the client skeleton.

Why: docs and prompts are edited on the server, and nodes hold **copies** that never pick up
updates themselves — "a fixed rule only applies to new nodes, old nodes keep the old mistake".
The fingerprint makes drift visible: nodes report theirs on register, PAD sees who is stale.

Only fingerprint files **both sides have, that should be re-pulled when changed**:
  - ld_agent.py / harness/*.py  behaviour code
  - harness/prompt.md.tmpl      executor prompt (the thing that drifts most easily)
  - JOIN.md                     protocol doc

A node's ld_config.yaml belongs to the node itself and **does not participate**.
"""
import hashlib
from pathlib import Path

FILES = [
    "ld_agent.py",
    "JOIN.md",
    "harness/prompt.md.tmpl",
    "harness/run_harness.py",
    "harness/run_script.py",
]

# Which files actually go into agent.bundle.tgz — the one packaging list, and server.py
# packs from it. Must ⊇ FILES, else the tree a node unpacks can never hash to the server's.
#
# To ship your own private scripts (say a pipeline for one specific machine) with the
# skeleton, add an entry here, or list comma-separated relative paths in PAD_BUNDLE_EXTRA.
BUNDLE_FILES = [
    "ld_agent.py",
    "bundle.py",
    "requirements.txt",
    "JOIN.md",
    "ld_config.node.yaml.example",
    "harness/run_harness.py",
    "harness/run_script.py",
    "harness/prompt.md.tmpl",
    "deploy/pad-node.service",
]

_missing = set(FILES) - set(BUNDLE_FILES)
if _missing:
    raise RuntimeError(f"FILES 里这些文件没打进 tarball: {sorted(_missing)}")


def bundle_hash(root) -> str:
    h = hashlib.sha256()
    for rel in FILES:
        f = Path(root) / rel
        h.update(rel.encode())
        h.update(f.read_bytes() if f.exists() else b"<missing>")
    return h.hexdigest()[:12]
