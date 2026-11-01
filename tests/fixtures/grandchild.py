import json, os, sys, time
time.sleep(float(sys.argv[1]))
wd = os.environ["PAD_WORK_DIR"]
open(os.path.join(wd, "result.json"), "w").write(json.dumps(
    {"summary": "grandchild (executor) finished",
     "report": "descendant completed after wrapper timed out"}))
