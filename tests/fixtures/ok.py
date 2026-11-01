import json, os
wd = os.environ["PAD_WORK_DIR"]
open(os.path.join(wd, "result.json"), "w").write(json.dumps({"summary": "ok done"}))
print("ok")
