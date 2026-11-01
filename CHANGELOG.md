# PAD 变更记录

只记「改完之后行为不一样」的事，供以后排查用。按时间倒序。

公开仓库保持这份记录里**没有**任何真实主机、域名、密钥和账号信息：
凡是这类内容，一律用 `<host>` / `<KEY>` / `<path>` 占位。

---

## 2026-11-01 MCP 通道 421 Misdirected Request

**现象**：ChatGPT 侧下任务一律失败，`/api/digest` 却一直是 200，节点心跳也正常 —— 只有 MCP 那条路挂。
反代访问日志里能看到

```
<proxy-ip> - - [date] "POST /pad/<KEY>/mcp HTTP/1.1" 421 19 "-" "openai-mcp/1.0.0"
```

**根因**：不是反代，是 `server.py` 里 MCP SDK 的 DNS-rebinding 防护。默认白名单只有
`localhost,127.0.0.1`，公网访问时 SDK 看到的 `Host` 不在白名单 → 直接回 421
（19 字节响应体）。普通 REST 路由不吃这层检查，所以 digest 正常，极具迷惑性。

**改法**：把真实访问用的 host 加进 `PAD_ALLOWED_HOSTS`（逗号分隔，反代送来的 `Host` 必须逐字匹配）：

```
PAD_ALLOWED_HOSTS=pad.example.com,localhost,127.0.0.1
```

**通用教训**：反代后的服务，**MCP 的 DNS-rebinding 白名单和反代的 `Host` 头必须对齐**。
换域名/换入口 IP 时，这类配置是最容易漏的一处；REST 通不代表 MCP 通，要分别验。

---

## 2026-10-26 输入文件从「只收图片」扩到「任意文件」

**问题**：`input_images` 只能带 PNG/JPEG/GIF/WebP，每任务 4 张、合计 5 MiB，且要 base64
塞进 MCP 参数。想从控制面往下发一份 PDF / 一份 Excel / 一段录音，做不到。

**改法**：`input_images` → `input_files`（旧名保留为别名，行为不变）。

| 项 | 旧 | 新 |
|---|---|---|
| 参数名 | `input_images` | `input_files`（`input_images` 仍可用） |
| 类型 | 仅图片 | 任意 MIME：pdf/docx/xlsx/csv/json/md/zip/mp4/mp3/srt/… |
| 数量 | 4 | 8（`PAD_INPUT_FILE_MAX_COUNT`） |
| 单文件 | 5 MiB | 10 MiB（`PAD_INPUT_FILE_MAX_BYTES`） |
| 合计 | 5 MiB | 15 MiB（`PAD_INPUT_FILE_MAX_TOTAL_BYTES`） |
| 校验 | 图片文件头 | 图片必须头对得上；其它按声明/扩展名记账 |
| 落盘 | `input_images/` | `input_files/`（`PAD_INPUT_IMAGE_ROOT` 仍认） |
| 文件名 | ASCII 白名单，中文变 `input-1` | 保留原名（中文可用），只剔路径与非法字符 |
| 下载端点 | `/api/task/<id>/input-image` | `/api/task/<id>/input-file`（旧路径仍注册） |
| 脑回读 | 无 | `get_input_file(task_id, name)`：图片原生、文本分页、二进制只给元数据 |
| 清理 | 无 | `sweep_input_files()`，任务终态后 14 天（`PAD_INPUT_FILE_TTL_DAYS`） |

**几个不显眼但会咬人的点**：

- **HTTP 头不能直接放中文名**。`Content-Disposition: attachment; filename="季度报告.pdf"`
  会让 Starlette 在编码头时抛错、节点下载 500。改成 RFC 5987：
  有 `filename*` 才带中文名（`_attachment_disposition`）。
- **老节点不能接到带文件的任务**。老 LD 的下载逻辑只认 `input-image` 且自带 4 张 / 5 MiB 校验，
  接了会整条判 failed。`claim_task` 在骨架指纹不一致时只放行「≤4 个、≤5 MiB、且全是 image/*」
  的任务，纯文本/PDF 一律不给它。
- **声明是图片但内容不是，必须拒**。上层看到 `mime_type=image/png` 就会当图片用，
  头是假的等于骗它。反过来：只按扩展名命名却没声明 MIME 的真图片，让文件头说了算（不拒）。
- **反代的 body 上限**默认很小（8m 级别），15 MiB 文件 base64 后约 20 MiB 会 413。
  `/pad/` 这个 location 要单独提到 `client_max_body_size 32m`。

**升级顺序不能反**：先部署 server，再重启节点 LD。反了的话老服务端不认识 `input_files`，
新节点只是白等。

---

## 2026-10-26 ChatGPT 附件直传（openai/fileParams）

**问题**：ChatGPT 能收用户附件（MP3/MP4/PDF），但 PAD 只能吃 `input_files[].data_base64`。
模型手里有附件，却没法把它变成任务输入 —— 要么放弃，要么把几 MB base64 塞进上下文。

**做法**：按 OpenAI 的 MCP 扩展 `openai/fileParams` 声明文件参数，让 ChatGPT 把附件
换成 `{download_url, file_id, mime_type, file_name}` 对象，PAD 在**本次调用内**自己取字节。

### 规范的硬要求（照抄官方，错一处就被 Scan Tools 拒收）

```jsonc
"_meta": {"openai/fileParams": ["files"]},   // 列参数名
"$defs": {"OpenAIFileParam": {
  "properties": {"download_url": {}, "file_id": {}, "mime_type": {}, "file_name": {}},
  "required": ["download_url", "file_id"],    // 必须正好这两项
  "additionalProperties": false
}}
```

- 四个属性必须**全部声明**（`mime_type` / `file_name` 可以不 required）
- `required` 必须**正好**是 `["download_url","file_id"]`：多一个、或把可选属性标成
  required，都会被拒
- 参数值必须"解析为文件对象或文件对象数组"

实现上用 pydantic 模型钉死形状，`@mcp.tool(meta={"openai/fileParams": ["files"]})` 声明。
两个工具都声明：`submit_task`（真干活）和 `probe_file_upload`（自检，只报字节数/SHA-256）。

### 关键时序约束

**`download_url` 只在本次工具调用期间有效。** 所以必须同步取字节落盘，
"存 URL 进任务、让节点稍后再拉"必然过期。节点侧因此**不需要任何改动**——
它看到的还是普通 `input_files`，这也解释了为什么这次是纯服务端改动。

### 安全（PAD 里唯一由外部 URL 触发本机出网的地方，按 SSRF 处理）

- 只允许 https；域名白名单默认 `oaiusercontent.com`/`openai.com`/`chatgpt.com`/`oaistatic.com`
- DNS 解析后拒绝内网/回环/保留地址（白名单挡不住 DNS 指向内网）
- **必须禁用 urllib 的自动重定向**：默认它会自己顺着 302 走完，手写的"每跳校验"
  永远不会触发 —— 一个 302 指向 `http://127.0.0.1:8791/` 就能把内网响应当附件存下来。
  这个漏洞是测试里发现的，不是设计时想到的。修法是 `_NoRedirect` 处理器把 3xx 变成
  普通错误，上层手动跟并逐跳校验。
- 边读边计数，超限立即中止（不先下完再判断）；连接 10s / 总时长 120s

`PAD_FILE_ALLOW_HTTP` / `PAD_FILE_ALLOW_LOOPBACK` 是**仅供单元测试**的开关，生产不设。

### 验证

- 单测 19 项（`tests/test_file_refs.py`）：schema 形状、字节一致、SSRF 四类、超限两条、
  path 拒绝、混合来源。全量 56 项通过。
- 端到端：起一个真 TLS 桩服务放一个 ffmpeg 生成的真 MP3 → 构造与 ChatGPT 同形的文件对象
  → MCP `submit_task(files=...)` → 节点收到字节数与 SHA-256 与源文件完全一致，
  并用 ffprobe 读出 3.000 秒时长（证明是真能解码的音频，不是碰巧字节相同的垃圾）。

### 首次实战的两个坑（真实调用暴露）

**坑 1：域名白名单把自家 CDN 拦了。**

ChatGPT 给的 `download_url` 实测会落在**云厂商对象存储**（不是文档示例里的
`files.oaiusercontent.com`）。写死 oai* 白名单 = 自己拦自己。

**决策：域名白名单默认不启用。** CDN 会换，域名列表永远追不上，而且这里的安全边界本来就不是
"信任哪些域名"——SSRF 的危害在于借服务端之手打内网。所以保留：只允许 https、
DNS 解析后拒绝内网/回环/保留地址、逐跳重定向重校验、体积与时长上限。
需要收紧时用 `PAD_FILE_URL_HOSTS`（默认空=不限制），别改代码。

**坑 2：真实错误被 SDK 吞掉，调用方只看到 INVALID_ARGUMENT。**

MCP SDK 的 `except Exception` 分支把异常当"崩溃"：模型只收到
`Error executing tool submit_task`，原因全留在服务端 traceback 里。调用方根本无从判断
是域名问题、格式问题还是网络问题。

修法：工具内部捕获 `ValueError` 并转成 `ToolError`（SDK 约定的"可预期失败"），
模型的 `is_error` content 里就能看到全文，例如：

```
文件接收失败：第 1 个输入文件拉取失败：下载地址解析到内网/保留地址，拒绝拉取: 169.254.169.254
```

顺带把"把本地路径塞进 input_files"这种误用也做成可读提示：
会明确说 `name='/mnt/data/xxx.mp3' 是个本地路径，PAD 读不到你那边的磁盘`。

**教训**：接入外部平台的扩展时，先把"错误能不能传回去"修好，否则每次排查都在猜。

---

## 2026-10-20 节点执行体可切换（claude / opencode / codex）

`harness/run_harness.py` 把"任务"翻译成某个 agent CLI 的无头调用，换执行体只是换一个参数：

| 执行体 | 实际命令 |
|---|---|
| `opencode` | `opencode run --standalone --auto --title pad:<task_id> <prompt>` |
| `claude` | `claude -p --dangerously-skip-permissions <prompt>` |
| `codex` | `codex exec -C <ws> --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox <prompt>` |

**opencode 的两个坑**：

- v2 删了 `--dir`，工作目录只能靠子进程 `cwd`；v1 还有 `--dir`，所以要按版本分派。
- v2 默认走**全用户共享**的后台服务，共享服务的事件流一旦被挤断，CLI 会
  `Transport: InvalidHTTPResponse ... /api/event` 直接 exit 1（而 session 还在服务端跑）。
  无头派发必须用 `--standalone` 起私有 server，生命周期跟着这次任务走。

**统一的收口**：三个执行体都按同一套约定 —— 提示词从 `harness/prompt.md.tmpl` 渲染，
完成/失败都写 `$PAD_WORK_DIR/result.json`，由退出码区分 done / failed，完整输出留
`$PAD_WORK_DIR/run.log`（永远不上传）。

`rc==0` 却没写 `result.json`（执行体"提前收尾"：把长任务放会话后台就结束了本轮，
进程退出把后台一并带走）时，opencode v2 / codex 按同一会话再催一次；claude 没有可续的
会话概念，跳过（`PAD_RESUME_ONCE=0` 可整体关掉）。

---

## 2026-09-18 取消必须传到执行面

**事故**：`close_task` 只改 PAD 里的状态，等于「控制面认为任务死了、执行面还在跑」：
被取消的长任务又继续跑了 22 分钟、占着单执行槽，queued 的任务接不进来。

**规则**：

- 节点在执行体运行期间轮询 `GET /api/task/<id>/status`（默认 15s）；看到 `cancelled`
  就杀整个进程组（SIGTERM → 宽限 → SIGKILL），释放槽位，**不再补交 done/failed**。
- 取消是终态：迟到的 `report` 不会再把它拽回来。
- 节点重启/后台回收遇到 `cancelled` 一律跳过，不能让已取消的任务「复活」出一份迟到的汇报。
- **超时 ≠ 取消**：会话软超时仍走 lease/harvest（descendant 可能还在干有效活），
  只有显式 `cancelled` 才杀。

---

## 2026-09-17 投完要读回确认

**事故**：节点汇报都拿到 200，服务端收到的却是空壳 payload。

**规则**：返回 200 ≠ 真的落库。投完 `GET /api/task/<task_id>`，看 `reports` 里有没有
你刚发的 `client_id`；没有就是没送到，重投到看见为止。`client_id` 是幂等键，
同一条重投服务端只记一次。

---

## 2026-09-17 骨架过期必须自己盯

**问题**：文档和提示词改在服务端，节点是**拷贝**，不会自动拿到更新。一条修好的规矩，
只对之后新接入的机器生效，老节点继续按旧规矩犯错。

**做法**：`bundle.py` 对 `ld_agent.py` / `harness/*.py` / `prompt.md.tmpl` / `JOIN.md`
取指纹。节点注册时上报，服务端一比就知道谁在跑旧代码（`nodes_bundle_stale`）。

**更新方式**（不会动 `ld_config.yaml`）：

```bash
curl -fsS "$BASE/agent.bundle.tgz" | tar xz -C "$PAD_HOME"
# 改完重启 LD 进程
```
