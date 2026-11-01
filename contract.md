# PAD 契约 v0

PAD = Persistent state + routing。它自己不做智能，只负责：谁在干什么、干到哪了，以及保存任务报告。

## 0. 分层

```
ChatGPT（脑）    →  下发 goal / constraints / acceptance，读取进度与任务报告
Product LD ×N    →  自主探索、拆解、执行、验证，最后提交 done / failed 报告
Work agent       →  真正干活，脑永远看不到它的日志
```

**规则**：goal 是目标，不是步骤。执行体应在给定约束内自行规划、尝试替代路径并验证结果；不等待上层拍板。

## 1. 下发基准：按 capability，不按机器

这是唯一需要记住的一条。脑**不能**说"给 gpu-1 发命令"。脑只能说"我要 `build.heavy` 这件事被完成"。

谁有 `build.heavy` 这个能力，谁去领。机器挂了、换机器、加机器，脑完全不用知道。

```jsonc
// 任务（脑 → PAD → 任意有该能力的 LD）
{
  "capability": "build.heavy",          // 路由键，必填
  "goal": "把仓库编译出来并跑通全部测试",
  "constraints": ["不要动生产配置", "只改 build/ 下的文件"],
  "acceptance": ["测试全绿", "产物可在 ./dist 找到"],
  "input_files": [{"name":"参考报告.pdf", "mime_type":"application/pdf", "data_base64":"..."}],
  "files": [{"download_url":"https://files.oaiusercontent.com/...", "file_id":"file-abc",
             "mime_type":"audio/mpeg", "file_name":"会议.mp3"}],
  "deadline": "2026-09-17T20:00:00+08:00",
  "priority": "normal"
}
```

**约束**：
- 没有 `machine_id` 字段，永不加
- 没有 `command` / `shell` 字段，永不加
- goal 是目标，不是步骤。步骤是 LD 的事

`input_files` 可选。两条带文件的路，按情况选一条：

**① `files`（ChatGPT 附件直传）** —— 用户在 ChatGPT 里上传附件后，ChatGPT 通过
`_meta["openai/fileParams"]` 把它换成 `{download_url, file_id, mime_type, file_name}`
对象。PAD 在**本次 MCP 调用内**自己取回字节落盘，模型不碰文件内容、不占上下文。
`download_url` 只在调用期间有效，所以不支持"存 URL 稍后再拉"。

**② `input_files`** —— 调用方已经拿到字节时用，每项 `name` / `mime_type` /
`data_base64`（也接受 `data:...;base64,...`）。旧名 `input_images` 等价。

两条路共用校验与落盘：每任务最多 8 个，单文件 ≤10 MiB、合计 ≤15 MiB。图片
（PNG/JPEG/GIF/WebP）必须文件头与声明一致；其余类型（pdf/docx/xlsx/csv/json/md/zip/
mp4/mp3/srt…）原样保存。PAD 保存原始字节，节点领到任务后下载到本机任务目录 `inputs/`，
执行提示、`PAD_INPUT_FILES` 环境变量和 `manifest.json` 会给出文件路径与 sha256。
`get_task` 返回文件元数据（不含字节），要回读用 `get_input_file(task_id, name)`：
图片回原生图片内容，文本按分页返回，其它二进制只给元数据。

**下载安全（`files` 通路）**：只允许 https + 域名白名单（默认 oai* 系）+
拒绝内网/回环解析结果；每一跳重定向都重新校验且禁用自动跟随；边读边计数超限即断；
连接 10s、总时长 120s 超时。这是 PAD 里唯一"由外部 URL 触发本机出网"的地方，按 SSRF 处理。

聊天附件不会自动变成 MCP 参数 —— 必须靠上面的 `files` 通路（ChatGPT 侧配合）或调用方
自己提供 base64。实际视觉理解还取决于节点执行器与模型是否支持图片。

**同机可并发**：节点按 capability 配 `concurrency`（默认 1 = 串行）。并发时 LD 只带
「还有空槽」的能力去 `claim`，所以领回来的活一定跑得了；取消（§4.1）、会话超时、
后台回收对**每个任务各自生效**。要不要并发的判据和跨机一样：会用账号登录态、
要写同一份文件/抢同一资源的（整套 daily），保持 1。

## 2. 汇报：两种，别混

### 2.1 状态流（心跳，常态，只为了让脑维持世界模型）

```jsonc
{ "level": "progress", "summary": "6篇 → 已发4，写作中2，预计16:20完成" }
```

一句话。脑不需要回应。

### 2.2 完成流

```jsonc
{
  "level": "done",
  "summary": "6篇全发",                       // 一行，给列表/心跳扫
  "payload": {
    "status": "done",
    "report": "验收汇报正文：①结论 ②这件大活做成了什么 ③拆出去的每件小事各自如何 ④风险/建议",
    "acceptance_results": [                    // 逐条回答验收条件，一条对一条
      { "item": "发布成功篇数==6", "passed": true, "evidence": "wechat 步骤 6/6，见逐步明细" }
    ],
    "artifacts": [                             // 产物：小的自动存一份到服务器，大的留本地
      { "name": "run.log", "bytes": 20481, "uploaded": true },
      { "name": "sites.jsonl", "rows": 10000, "schema": "url,title,status", "uploaded": false,
        "path": "/home/x/pad-ld/work/t-xxx/sites.jsonl", "bytes": 482103221 }
    ],
    "_acceptance_check": { "total": 3, "covered": 3, "missing": [] }   // 服务端标注
  }
}
```

**报告怎么写**：像把活派给几个人、干完写回给老板的验收汇报 —— 大活做成了什么、
每件小事各自如何。**长短不设死，讲清楚为准**；不许为凑字数灌水，也不许一句汇总带过。

**压数据量，不压答案**：批量/原始数据留本地只带统计；上层点名要的字段一个字不许省。
大文件不是丢了，是"留了指针"：`name / rows / schema / bytes` 都在，
真要细节用 `get_artifact(task_id, name, offset)` 取片段（默认 8KB，offset 翻页）。

**验收覆盖度**：服务端会在 done 时逐条核对，缺哪条写进 `_acceptance_check.missing`。
它只判"有没有回答"，**不判真伪** —— 真伪要靠另一个节点跑 `verify.article` 独立复核。

### 2.3 失败流

```jsonc
{ "level": "failed", "summary": "抓取源全挂，0篇产出",
  "payload": { "status": "failed", "report": "已尝试的路径、失败原因、未完成项与证据" } }
```

## 3. 执行与失败报告

遇到阻碍时，执行体在既定 goal 和 constraints 内自行调查、验证假设并尝试合理替代路径，不创建升级请求，也不等待上层拍板。

如果仍无法完成，保留已经完成的部分，并提交 `level=failed` 的任务报告：说明已尝试的路径、失败原因、未完成的目标或验收项、证据位置和可行的后续建议。不得把失败伪装成成功；报告仍然是任务的最终交付物。

高副作用操作只在 goal 明确要求且 constraints 允许时执行；若授权不足，跳过该操作并在报告中说明，不升级等待。

## 4. 执行纪律

| 层 | 负责内容 | 约束 |
|---|---|---|
| LD → 脑 | 进度、done / failed 报告、产物指针 | 不传原始日志和堆栈，细节留在产物中 |
| 脑 → LD | goal、constraints、acceptance | 描述目标和边界，不规定执行步骤 |

**压缩但留指针**：往上走的是判断，往下钻的把手留着（`get_task` 能看到该任务的汇报流）。

### 4.1 取消必须传到执行面（2026-09-18 事故）

`close_task` 只改 PAD 里的状态，等于「控制面认为任务死了、执行面还在跑」：
Mac 上被取消的 `daily.all` 又继续跑了 22 分钟、占着单执行槽，queued 的排查任务接不进来。

规则：

- **节点在执行体运行期间轮询 `GET /api/task/<id>/status`**（默认 15s）；看到 `cancelled`
  就杀整个进程组（SIGTERM → 宽限 → SIGKILL），释放槽位，**不再补交 done/failed**。
- 取消是终态：迟到的 `report` 不会再把它拽回来（服务端只对非终态任务更新状态）。
- 节点重启/后台回收（`recover_orphans` / `sweep_pending`）遇到 `cancelled` 一律跳过，
  不能让已取消的任务「复活」出一份迟到的汇报。
- **超时 ≠ 取消**：会话软超时仍走 lease/harvest（descendant 可能还在干有效活），
  只有显式 `cancelled` 才杀进程。

## 5. 脑的工具面

```
get_digest(recent_limit=10)         全局态势：节点心跳、每台机最近任务 id（时间倒序）、在跑任务
submit_task(capability, goal, ...)  下发（可用 input_files 随任务带文件）
get_task(task_id)                   单任务：状态 + 汇报流 + 产物清单 + 输入文件清单
get_input_file(task_id, name)       打开随任务下发的输入文件（图片原生、文本分页）
get_artifact(task_id, name, offset) 打开一个产物看正文（按字节分页；name 留空只列清单）
close_task(task_id, reason)         取消任务
```

`get_task` 负责「告诉我有什么」，`get_artifact` / `get_input_file` 负责「我要打开这个看看」——
前者给元数据（name/bytes/rows/schema/mime_type），后两者按 `offset`/`limit`（默认 8KB）
取正文那一段，整包原始数据不搬进上下文。

**随任务下发文件（`input_files`）**：文件在 PAD 里的身份是「任务输入」，和节点回传的
「产物」是两张表、两条路。脑下发时给 base64；节点领到后落到 `inputs/` 再执行；
脑要回看自己发过的文件，用 `get_input_file`，不要指望 `get_artifact` 能查到——
产物表里没有它。

不给 `run`、不给 `shell`、不给 `machine_id`。

## 6. capability 命名（当前）

```
build.heavy        重型构建/编译/训练
scrape.batch       批量抓取（只读，可多台并行）
research.brief     出选题简报
site.deploy        站点部署
verify.article     独立校验（由不同节点跑）
publish.blog       发布（任何要账号登录态的发布，都只有一台机器能声明）
```

名字只是例子：`get_digest` 里每台机器报什么能力，就按什么派活。

**SOP 能力 vs 开放目标（2026-09-18 教训）**：capability 名携带的既有 workflow 语义越强，
脑越容易「错误套 SOP」。像 `mac.daily.all`（本项目私有的固化能力，示例而已） 这种名字，隐含的就是「按固化好的 daily 流程跑**今天**」；
当意图其实是「提前做明天 / 换个做法 / 先摸清情况」时，套它就会把日期和候选池一起固化，
产出跟昨天雷同。约定：

> **「照常 / 按原规划」才用固化能力；一旦是提前、变更、实验、研究、优化这类新目标，
> 默认下发开放式 `*.generic`，把 planning 留给执行体。**

固化入口不该被当成「任意日期 / 任意变体」的通用入口用。
