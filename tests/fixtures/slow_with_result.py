import json, os, time
wd = os.environ["PAD_WORK_DIR"]
open(os.path.join(wd, "result.json"), "w").write(json.dumps(
    {"summary": "already computed",
     "report": "result existed before session timeout",
     "artifacts": [{"name": "data.csv", "path": "data.csv", "rows": 3,
                    "schema": "a,b", "upload": True}]}))
open(os.path.join(wd, "data.csv"), "w").write("a,b\n1,2\n")
time.sleep(20)
