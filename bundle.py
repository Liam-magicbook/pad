"""客户端骨架的版本指纹。

为什么需要：文档和提示词改在服务端，节点是**拷贝**，不会自动拿到更新。
结果就是"修好的规矩只对新节点生效，老节点继续按旧规矩犯错"。
这个指纹让 drift 可见：节点注册时上报自己的指纹，PAD 一比就知道谁在跑旧代码。

只对**双方都有的、且改了就该重拉**的文件取指纹：
  - ld_agent.py / harness/*.py  行为代码
  - harness/prompt.md.tmpl      执行体提示词（最容易漂的就是它）
  - JOIN.md                     协议文档

节点的 ld_config.yaml 是它自己的，**不参与**指纹。
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

# agent.bundle.tgz 里实际打哪些文件 —— 唯一的出包清单，server.py 照这个打包。
# 必须 ⊇ FILES，否则节点从包里解出来的树永远算不出服务端的指纹（恒假报警）。
#
# 想把自己的私有脚本（比如某台机器专用的流水线）也随骨架下发，就往这个列表里加一项，
# 或者在环境变量 PAD_BUNDLE_EXTRA 里给逗号分隔的相对路径（默认不含任何私有文件）。
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
