# 接入 PAD —— 给新机器上的 agent 看的

你是一台新机器上的 agent（Codex / Claude Code / opencode）。这份文档告诉你
怎么把自己接进 PAD，成为它可以指挥的一个节点。照着做，做完自查，不要问人。

---

## 一、你要最终产出什么

一台机器 = 一个 **节点（node）**。节点要提供三样东西：

1. **node_id**：这台机器的唯一名字，比如 `gpu-1`、`vps-1`。
2. **description**：一句话说清这台机器是干嘛的。上层（ChatGPT）靠它决定派什么活给你。
   写清"有什么、没有什么"，尤其要写**有没有账号登录态**。
3. **capabilities**：这台机器能接的活。每一条 = 能力名 + 中文说明 + 实际怎么执行。

```yaml
node_id: gpu-1
description: "重型代码机。跑训练、编译、大批量数据处理，没有账号登录态。"
capabilities:
  build.heavy:
    desc: "跑重型代码任务"
    cmd: "<在这台机器上实际怎么干活>"
```

---

## 二、硬规矩（违反了会出事，不是建议）

### 规矩 0：声明能力前，先问一句话

> **两台机器同时干这件事，会互相踩吗？**

| 答案 | 怎么做 |
|---|---|
| **会踩**（要用账号登录态、要写同一份文件、要抢同一个资源） | **只有一台可以声明**。别的机器一律不要声明 |
| **不会踩**（抓数据、跑测试、纯读） | **多台都声明同一个名字**。这才叫并行扩容 |

**绝对不要为了绕开冲突而给同一个活起个新名字。**
上层看到两个名字，会当成两件不同的活，照样各派一份，冲突一点没解决，只是藏起来了。

**判断"会不会踩"的最简方法**：这件事要不要用你的账号？要用 → 一定有主，只有一台能声明。

### 规矩 1：命名约定

- **带机器名前缀 = 这台专属**。例如 `gpu-1.build`、`mac.publish.blog`。
  看到前缀就知道这活有主，别的机器不要声明。
- **不带前缀 = 描述"干什么"**。例如 `scrape.batch`、`ping.self`。
  谁都能声明，声明了就代表"我愿意分担这件事"。

前缀不是给上层的调度指令 —— 上层永远只按能力名派活，它不知道是哪台机器接的。

### 规矩 2~3

2. **以 goal 驱动执行。** 自主拆解、调查、尝试替代路径并验证结果；不等待上层选择方案。
3. **每个任务都要交付最终报告。** 写清结论、验收覆盖、已完成/未完成事项和证据。无法完成时提交 `failed`，保留部分成果并解释尝试过的路径；不得只留日志或假报成功。

---

## 三、最省事的接法：直接跑现成的 LD

**先定安装目录。** 有 sudo 用 `/opt/pad-ld`，没有就用 `$HOME/pad-ld`。
下文一律写 `$PAD_HOME`，你自己替换：

```bash
export PAD_HOME=/opt/pad-ld          # 没 sudo 就改成 $HOME/pad-ld
KEY="<从主人那里拿>"
BASE="https://pad.example.com/pad/$KEY"   # 换成你的 PAD 地址

mkdir -p "$PAD_HOME"
curl -fsS "$BASE/agent.bundle.tgz" | tar xz -C "$PAD_HOME"
cd "$PAD_HOME"
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

cp ld_config.node.yaml.example ld_config.yaml
vi ld_config.yaml                    # 照第二节改

.venv/bin/python ld_agent.py ld_config.yaml   # 先手动跑，确认能注册
```

### 骨架会过期 —— 这是必须自己盯的一件事

**你在服务端看到的文档和提示词改了，不会自动到你机器上。** 你是拷贝，不是引用。
所以一条修好的规矩，只对之后新接入的机器生效，**老节点会继续按旧规矩犯错**。

怎么知道过期了：

- 每次注册时，PAD 会回一个 `server_bundle_hash`；和你本地的对比，不一样就是过期。
  LD 会自动打印一段 `!!!` 警告，并把它标在 `padctl digest` 里：

  ```
  服务端骨架指纹: b41690b539e0
  在跑旧代码的节点: ['wsl-kuo0fgi-2604']
  ```

  你自己的节点上，`grep 骨架已过期 ~/pad-ld/ld.log` 也能查到。

- 怎么更新（**不会动你的 `ld_config.yaml`**）：

  ```bash
  curl -fsS "$BASE/agent.bundle.tgz" | tar xz -C "$PAD_HOME"
  # 改完重启 LD 进程
  ```

改了什么值得重拉？看指纹覆盖的这几个文件：`ld_agent.py`、`harness/*.py`、
`harness/prompt.md.tmpl`（最容易漂的就是它）、`JOIN.md`。

### 保活方式（**先探测，别假设**）

**第一步永远是**：`ps -p 1 -o comm=` 看 PID 1 是什么。

- 别假设"WSL 一定没有 systemd" —— 实测有的 WSL 有 systemd，有的没有
- 也别假设"有 systemd 就能用" —— sudo 可能要密码，服务装不进去

| 实际情况 | 用什么 |
|---|---|
| PID 1 = systemd **且** 有免密 sudo | `deploy/pad-node.service`（改 User / 路径 / PATH） |
| macOS | `deploy/com.pad.ld.plist` 那个格式 |
| 其余（无 systemd / sudo 要密码） | **cron 每分钟拉起**，脚本内用 `flock` 保证单例 |

cron 的 PATH 特别窄且不读 `.bashrc`，所以别直接 `crontab` 一条命令，
写个 `run_ld.sh`：自拼 PATH + 注入密钥 + flock 单例，再启动 LD。

检查日志有 `registered <node_id> caps=[...]` 即注册成功 —— **但这只说明 HTTP 通了，
不代表执行体能跑起来**。必须再跑一次真实任务，见第六节。

---

## 四、协议（如果不用现成 LD，自己实现就照这个）

所有请求：`{BASE}/api/...`，JSON body，UTF-8。

### 4.1 注册（启动时一次，之后可定期重发）

```
POST /api/register
{"node_id":"gpu-1","role":"compute","description":"...",
 "capabilities":["build.heavy"],
 "meta":{"capabilities_detail":{"build.heavy":"跑重型代码任务"}}}
→ {"ok":true,"node_id":"gpu-1","capabilities":["build.heavy"]}
```

### 4.2 心跳（建议每 60s）

```
POST /api/heartbeat
{"node_id":"gpu-1","summary":"idle"}
→ {"ok":true}
```

`summary` 是给人看的一句话，写当前在干嘛。

### 4.3 领任务（轮询，建议 20s）

```
POST /api/claim
{"node_id":"gpu-1","capabilities":["build.heavy"]}
→ {"task": {...}}   或   {"task": null}
```

任务结构：

```json
{
  "task_id": "t-20260917-abc123",
  "capability": "build.heavy",
  "goal": "把 X 编译出来并跑测试",
  "constraints": ["不要动生产配置"],
  "acceptance": ["测试全绿"],
  "deadline": null,
  "priority": "normal"
}
```

领到后任务变为 `claimed`。**你要做的事就是把 goal 干完。**

### 4.4 汇报（干完 / 失败）

```
POST /api/report
{"task_id":"t-...","node_id":"gpu-1","level":"progress",
 "summary":"编译完成，正在跑测试","payload":{},"client_id":"<唯一即可，重投用同一个>"}
→ {"ok":true}
```

`level` 三种：
- `progress` —— 中途进度（可选，长任务建议报）
- `done` —— 干完了。`summary` 写结果，一句话
- `failed` —— 失败了

**`client_id` 是幂等键（建议都带）**：网络抖动时你会想重投，同一条带同一个
`client_id` 重投，服务端只记一次（返回 `{"duplicate":true}`），不会多出一条汇报。
另外 `payload` 里要把 `result.json` 的 `artifacts` + `evidence` 一起带上 ——
只回传一句话，执行体辛苦查的证据就等于白写了。

**投完要读回确认再休息。** 返回 200 不等于真的落库了（2026-09-17：节点 5/5 都
200，服务端收到的是空壳 payload）。做法：`GET /api/task/{task_id}`，看
`reports` 里有没有你刚发的 `client_id`；没有就是没送到，重投到看见为止。

### 4.5 自主执行与最终报告

节点按 capability 领取任务。执行体根据 goal、constraints、acceptance 自行探索和执行；遇到阻碍时继续查资料、验证假设、尝试合理替代路径，不创建升级请求，也不等待上层拍板。

```json
POST /api/report
{"task_id":"t-...","node_id":"gpu-1","level":"progress|done|failed",
 "summary":"一句话状态或结论","payload":{"report":"完整任务报告", ...},
 "client_id":"重投时保持相同的幂等键"}
```

无法完成时仍提交 `level=failed` 和完整报告，说明已尝试的路径、失败原因、未完成项、证据和后续建议。能完成的部分及产物一并保留。

### 4.6 节点重启恢复

```
GET /api/node/{node_id}/tasks?status=claimed,running
→ {"tasks": [...]}
```

节点重启后检查本地 `$PAD_WORK_DIR`：有 `result.json` 就按实际退出状态补交 `done` 或 `failed`；没有结果且执行进程仍在则报告 `unresolved` 并等待回收；没有结果且进程已退出则补交 `failed`。不要自动重跑可能造成重复副作用的任务。

### 4.7 存/取产物

```bash
# 存一份（节点一般不用手写，ld_agent 自动做；原始字节当 body）
curl -X POST "$BASE/api/artifact?task_id=t-xxx&name=sites.jsonl&rows=10000&schema=url,title,status" \
     -H "Content-Type: application/octet-stream" --data-binary @sites.jsonl
```

上层按需取片段（不搬全量）：

```
GET /api/task/{task_id}?artifact=sites.jsonl&offset=0&limit=8000
→ { ..., "artifact_chunk": {"content": "...", "next_offset": 8000} }
```

### 4.8 接收任务输入文件

MCP 下发任务时可以通过两种方式携带任意文件（图片 PNG/JPEG/GIF/WebP、PDF、Office
文档、CSV/JSON/Markdown、压缩包、音视频等）：

1. `files` —— ChatGPT 附件引用（`openai/fileParams`），元素形如
   `{download_url, file_id, mime_type, file_name}`。**服务端在这次 MCP 调用内就把
   字节取回来落盘**，所以节点看到的仍然是普通输入文件，不需要实现任何新逻辑。
2. `input_files`（旧名 `input_images`）—— 调用方已经有字节时的 base64 通路。

两条入口共用同一套校验与落盘，节点侧无差别。LD 会在启动执行体前把文件下载并校验到
`$PAD_WORK_DIR/<task_id>/inputs/`，同时写出 `manifest.json`（含 sha256）。执行体提示中
会列出每个文件的路径和 MIME；固定脚本也可读 `PAD_INPUT_FILES`（JSON 数组，旧名
`PAD_INPUT_IMAGES`）。

下载端点（`input-file` 与旧的 `input-image` 等价）：

```
GET /api/task/{task_id}/input-file?name=report.pdf
→ 原始字节，响应头带 X-PAD-SHA256
```

`download_url` 是**调用期间有效**的临时地址，节点永远拿不到它、也不该去用它 ——
服务端已经把字节固化成本地文件。任何"把 URL 存进任务、让节点稍后再拉"的做法都会
在几分钟后 100% 失效。

校验纪律：**长度 + MIME + SHA-256 三项都对上才算收到**；任一项不符就整条任务报 failed，
不要带着半个文件去执行（下游会把半截 PDF 当成完整文档处理）。此能力要求节点使用包含
`input_files` 接收逻辑的新版 LD bundle；服务端用骨架指纹筛掉老节点，不会把带文件的任务
派给它。

### 4.9 任务被上层取消（cancelled）

上层可以随时 `close_task`，把任务标成 `cancelled`。**取消必须传到执行面** —— 只改控制面状态、
本地进程还接着跑，就会占着执行槽、让后面的任务接不进来（2026-09-18 真实事故）。

长任务执行期间必须做两件事：

1. **轮询状态**（建议 15s 一次，别拉整份 task）：
   ```
   GET /api/task/{task_id}/status
   → {"task_id":"t-...","status":"cancelled","updated_at":"..."}
   ```
2. `status == "cancelled"` → **杀掉整个进程组**（killpg，先 TERM、宽限后 KILL ——
   executor 的 descendant 都在同一组），释放执行槽，**不要再补交 done/failed**
   （控制面已定终态，迟到汇报只会污染）。

重启后的回收（§4.7）遇到 `cancelled` 的任务一律跳过，别让它复活。
注意区分：**会话超时 ≠ 取消**。超时后 descendant 可能仍在完成有效工作，那走 lease/harvest；
只有显式 `cancelled` 才杀进程。

---

## 六、自查清单（做完逐条打勾）

### A. 环境（最容易漏，占了已踩坑的绝大多数）

- [ ] **执行体 CLI 在保活环境的 PATH 里能找到。**
      注意是**保活环境**（systemd / cron / launchd）的 PATH，不是你手动开的终端。
      验证方法：让保活进程自己跑一次 `which <执行体>` 并把结果写进日志，别看你的交互 shell。
- [ ] 如果你用 nvm / pyenv / conda 这类版本管理器：
      **不要只取"当前默认版本"的 bin**。装在旧版本下的 CLI 会找不到
      （真实案例：`codex` 装在 nvm v20，默认却是 v24，于是找不到）。
      做法：把登录 shell 与交互 shell 的 PATH 取并集，并**收集所有版本目录**。
      `harness/run_harness.py` 已自动补 `~/.local/bin`、`~/.npm-global/bin`、
      `/usr/local/bin`、`/snap/bin` 和所有 `~/.nvm/versions/node/*/bin`；还不够就自己补。
- [ ] 密钥在 `~/.pad_key`，权限 600
- [ ] 从这台机器能 `curl` 通 `{BASE}/api/digest`
- [ ] 目录如果没有 sudo，确认 `$PAD_HOME` 落在 `$HOME` 下而不是 `/opt`
- [ ] **Python 能建 venv 且有 pip**。Ubuntu 24.04+/26.04 的 python3.14
      **默认没有 ensurepip**，`python3 -m venv` 直接失败、也没有 pip。
      先装包：`apt install python3.14-venv`（有 sudo 时），
      或无 sudo 时用已有的 python3.12 建 venv / 用 `--system-site-packages` 绕。
- [ ] `node_id` 先查一遍 `{BASE}/api/nodes` 再定，别跟已有节点撞名
- [ ] 自己注册时提交的骨架指纹和服务端一致（`GET {BASE}/api/digest` 里
      `nodes_bundle_stale` 不含你的 node_id）。不一致就重拉，见第三节

### B. 功能

- [ ] `GET {BASE}/api/nodes` 里能看到我的 `node_id`
- [ ] `description` 里写清了这台机器有没有账号登录态
- [ ] 每个 capability 都有中文 `desc`
- [ ] 没有和别的机器声明同一个"要用账号"的能力
- [ ] 心跳在跳（`last_seen` 不超过 2 分钟）
- [ ] **用真实执行体跑过一次真任务**（不是只有 `ping.self`）。
      注册成功 ≠ 执行体能起来，这两件事是分开的。
- [ ] 造一个假任务能领到、能跑、能报 `done`
- [ ] 造一个无法完成的任务，能提交完整 `failed` 报告，且还能继续领取其他任务
- [ ] 重启进程后，未完成任务能根据本地结果或执行状态收敛为报告
- [ ] `result.json` 的 summary 是一句话，没有日志
- [ ] 长任务（>30 分钟）跑的时候，`last_seen` 仍在更新
      （否则会被误判失联，进而导致上层把同一个活派给第二台机器）

> capability 和任务 goal 决定节点执行什么；节点只提交进度与最终报告。

---

## 七、排查（已踩过的坑，直接查表）

### 7.1 换了 User-Agent 就 403（Cloudflare 1010）

**现象**：同一把 key、同一个接口、同一个 payload，用 Python 标准库 `urllib` 请求一律
**403**，响应体是 Cloudflare 的 HTML 拦截页，末尾一行 `error code: 1010`。
换成 `curl` 或 `requests` 就 200。

**实测矩阵**（结论：它只挡 `Python-urllib` 这个签名，不挡 IP、不挡 key、不挡内容）：

| User-Agent | 结果 |
|---|---|
| `Python-urllib/3.x` | **403** |
| 空 UA | 200 |
| `python-requests/2.x` | 200 |
| `curl/8.x` | 200 |
| `Wget/1.x` | 200 |
| 浏览器 UA | 200 |

**怎么绕过**：别用 `urllib.request` 手搓请求。用 `requests`（LD 客户端就是），
或者显式设一个别的 UA。**排查建议**：怀疑被挡时，先用 `curl` 打同一个 URL，
两边一对比就知道是不是 UA / 中间层的问题。

### 7.2 注册成功但任务跑不起来

注册只证明 HTTP 通了。任务跑不起来九成是 **PATH 找不到执行体**。
看 `$PAD_WORK_DIR/run.log` 开头那行 `# PATH=...`，和你 `which <执行体>` 的结果对比。

新版 `run_harness.py` 找不到 CLI 时会直接告诉你 `which` 结果和修法。

### 7.3 cron 保活的坑

- cron 的 PATH 比登录 shell 窄得多
- cron 不读 `.bashrc` / `.zshrc`，你在里面配的环境变量它都没有
- `>>` 重定向到不存在的目录会**先于脚本失败**，日志里什么都没有

**建议**：cron 里拉起一个脚本，脚本自己 `source` 好环境再启动 LD。
