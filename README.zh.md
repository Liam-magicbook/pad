# PAD

[English](README.md) · **中文** · [Français](README.fr.md) · [日本語](README.ja.md) · [Русский](README.ru.md)

**把你的机器接成一个能被 ChatGPT / Claude 指挥的执行集群。**
一个进程，一个 SQLite，两扇门——模型无状态，状态全在 PAD。

<img src="docs/preview/hero.gif" width="880" alt="四格动图：一键起步 / 舰队态势 / MCP 工具面 / 任务报告">

<sub>四格都是真实运行的截帧：左上 `./quickstart.sh` 逐行点亮、右上 `padctl digest`、左下 MCP 工具面、右下任务报告。由 `docs/make_preview.py` 从本机真实输出生成，不是设计稿。</sub>

**论文**：[`paper/pad.pdf`](paper/pad.pdf) —— 6 台机器、938 个真实任务、835 机时的实证报告（ICML Template）。

---

```
/pad/<KEY>/mcp        给脑（ChatGPT / Claude）—— 9 个任务管理与报告读取工具
/pad/<KEY>/api/...    给每台机器的 LD agent —— 注册 / 领活 / 进度与最终报告
```

模型不需要记得昨天干了什么、哪台机器在跑什么、任务卡在哪一步 —— 这些它记不住，也不该记。
PAD 把它们放进 SQLite，模型每次只问一句"现在什么情况"，拿到一份摘要，继续往下指挥。

![PAD 是什么](docs/preview/architecture.jpg)

## 一键起步

```bash
git clone https://github.com/Liam-magicbook/pad.git && cd pad
./quickstart.sh
```

三分钟以内，它会：建虚拟环境 → 装依赖 → 生成密钥 → 起服务端 → 起一台本地节点
→ 打一个真任务 → 把任务报告打给你看。

![一键起步](docs/preview/quickstart.jpg)

跑完你会拿到一个 MCP 地址，填进 ChatGPT 就能用了（见 [接进大脑](#接进大脑)）。
停掉：`./quickstart.sh --stop`。

要求：Python 3.10+、能建 venv。没有其他依赖，不需要 Docker、不需要数据库服务。

## 它解决什么问题

你在三台机器上跑着三种活：一台 Mac 有浏览器登录态、一台云主机跑长时间构建、
一台小机器专门抓数据。你在 ChatGPT 里说"把报告出来"，希望它自己知道该派给谁。

**没有 PAD**，你得自己在对话里描述每台机器的状态，模型还得记住。换一个会话，全部重新交代。

**有 PAD**，你说"出一份报告"，模型调一次 `get_digest`，看到：

```
节点:
  mac-1       running 2 [publish:12min, build:0min]
    caps: publish.blog, build.heavy
  vps-1       idle
    caps: scrape.batch
在跑任务 (2):
  t-20261102-a3f2   running   publish.blog  今天发 6 篇
今日完成: 7
```

然后它派活到 `scrape.batch`，因为 `vps-1` 正好空着 —— 而**它从来不需要知道机器名**。
机器挂了、换机器、加机器，模型侧一行都不用改。

![digest](docs/preview/digest.jpg)

## 接进大脑

PAD 对外的唯一入口是 MCP。给模型的工具只有 9 个，没有 `run`、没有 `shell`、没有 `machine_id`：

```
get_digest(recent_limit=10)          全局态势：谁在线、在跑什么、今天完成多少
submit_task(capability, goal, …)     下发任务（可带文件）
get_task(task_id)                    单任务：状态 + 汇报流 + 产物清单
get_artifact(task_id, name, offset)  打开产物看正文（分页）
get_input_file(task_id, name)        打开随任务下发的文件
close_task(task_id, reason)          取消任务（会真的杀本地进程）
probe_file_upload(files)             文件通路自检
get_artifact_delivery / observe      产物直链与抽帧（可选扩展）
```

![MCP 工具面](docs/preview/mcp.jpg)

**ChatGPT**：Settings → Connectors（Developer mode）→ Create → MCP Server URL 填
`http://<你的主机>:8790/pad/<KEY>/mcp` → Scan tools → 确认出现上面这些工具。
新开一个对话，在工具菜单里启用 PAD。

**Claude Code / 其他 MCP 客户端**：

```json
{
  "mcpServers": {
    "pad": { "type": "http", "url": "http://127.0.0.1:8790/pad/<KEY>/mcp" }
  }
}
```

本机试通了不代表外网能用。要让手机或别的网络访问，见 [部署](#部署)。

## 加一台机器

一台机器 = 一个节点 = 一个 YAML。**不需要改 PAD 一行代码。**

```bash
# 在新机器上
export PAD_HOME=$HOME/pad-ld
BASE=http://<你的主机>:8790/pad/<KEY>

mkdir -p "$PAD_HOME"
curl -fsS "$BASE/agent.bundle.tgz" | tar xz -C "$PAD_HOME"
cd "$PAD_HOME"
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

cp ld_config.node.yaml.example ld_config.yaml
vi ld_config.yaml                 # 改 node_id / capabilities / pad_url
.venv/bin/python ld_agent.py ld_config.yaml
```

`curl` 回来的 `agent.bundle.tgz` 是**服务端当前那份** LD 与提示词 —— 不用 clone 仓库，
也不会有"我这台跑的是三个月前的逻辑"这种问题。服务端改了骨架，节点注册时会收到指纹不一致的警告，
一条命令重拉即可（不动你的 `ld_config.yaml`）：

```bash
curl -fsS "$BASE/agent.bundle.tgz" | tar xz -C "$PAD_HOME" && <重启 LD>
```

详细的接入协议、自查清单、以及**踩过的坑**在 [JOIN.md](JOIN.md)。

### 能力的两种写法

```yaml
capabilities:

  # ① 确定性活：直接调一条命令
  scrape.batch:
    desc: "批量抓数据，只读"
    cmd: "cd ~/projects/scraper && python3 crawl.py --out $PAD_WORK_DIR/data.json"
    timeout_min: 120

  # ② 需要判断的活：交给本机 agent CLI 无头跑
  build.heavy:
    desc: "跑重型代码任务"
    cmd: "python3 /opt/pad-ld/harness/run_harness.py opencode"
    timeout_min: 240
    concurrency: 2
```

`run_harness.py` 把任务翻译成某个 CLI 的调用，**换执行体只是换一个参数**：
`opencode` / `claude` / `codex`。全部适配代码就是一个 `build_cmd()`。
PAD 本身不知道你用什么模型 —— 它只发"要干什么"，适配在节点这边，所以每台机器可以不一样。

无论哪种写法，收口都是同一个：任务完成或失败，都在 `$PAD_WORK_DIR/result.json` 里写
`done` / `failed`、一段验收说明书、以及逐条验收条件的证据。**没有 `result.json` 就不算完成**，
退出码 0 也不行。

一条 `echo` 也能跑到 `done`，但服务端会标"验收未回答"（`_acceptance_check.covered=0`）——
不是坏了，是它诚实地说这份报告没法自证。要证据就套一层 `harness/run_script.py`，
它把 stdout、退出码、日志尾巴收成标准 `result.json`。

### 加能力之前先问一句

> 两台机器同时干这件事，会互相踩吗？

| 答案 | 怎么做 |
|---|---|
| **会踩**（要用账号登录态、写同一份文件、抢同一个资源） | **只有一台**可以声明 |
| **不会踩**（抓数据、跑测试、纯读） | 多台都声明**同一个名字**，这才叫并行扩容 |

绝对不要为了绕开冲突给同一个活起个新名字 —— 上层会当成两件不同的活，照样各派一份，
冲突一点没解决，只是藏起来了。

## 部署到公网

本机 `127.0.0.1` 只够自己玩。要让手机、别的网络、或者 ChatGPT 的云端访问，需要三层：

```
公网 → 反代(nginx/caddy, TLS) → 127.0.0.1:8790 (uvicorn)
```

要点（每一条都踩过）：

| 事项 | 说明 |
|---|---|
| **`PAD_ALLOWED_HOSTS`** | MCP 的 DNS-rebinding 白名单，**逐字匹配 `Host` 头（含端口）**。反代送来的 host 不在白名单 → 421，而 REST 接口照常 200，极难排查 |
| **`PAD_KEY`** | 就是认证。URL 里带着它，等于一把可撤销的长期密钥。别贴公开场合 |
| **反代 body 上限** | 默认很小，带文件的任务会 413。`/pad/` 这个 location 单独提到 `client_max_body_size 32m` |
| **别重启服务端** | `systemctl restart pad` 会打断正在跑的任务（进程组跟着死）。要重启挑空窗 |

```bash
# 服务端
PAD_KEY=$(cat data/.pad_key) \
PAD_DB=/var/lib/pad/pad.db \
PAD_ALLOWED_HOSTS=pad.example.com,localhost,127.0.0.1 \
.venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port 8790
```

systemd 单元在 `deploy/pad.service`，节点守护在 `deploy/pad-node.service`（Linux）
和 `deploy/com.pad.ld.plist`（macOS）。两份模板里 `CHANGE_ME` 的地方都标了。

## 随任务下发文件

模型不能把几 MB 的 base64 塞进上下文。所以有两条款路：

**① `files`（ChatGPT 附件直传）** —— 用户在对话里传了附件，ChatGPT 通过
`_meta["openai/fileParams"]` 换成 `{download_url, file_id, mime_type, file_name}`，
服务端**在本次调用内**自己取回字节，模型完全不碰内容。

**② `input_files`** —— 调用方已拿到字节时用，`{name, mime_type, data_base64}`。

两条路共用同一套校验落盘（每任务 8 个、单个 ≤10 MiB、合计 ≤15 MiB），节点领到任务后
在 `inputs/` 下看到原文件 + `manifest.json`（含 sha256）。类型不限：图片、PDF、Office、
CSV/JSON/Markdown、压缩包、音视频、字幕。

命令行不想手搓 base64：

```bash
./padctl.py submit --capability build.heavy --goal "按这份报告改方案" \
  --file ~/Downloads/季度报告.pdf --file ~/Downloads/数据.xlsx
./padctl.py inputs t-20261102-xxxxxx          # 这个任务带了哪些文件
./padctl.py input  t-20261102-xxxxxx --out .  # 下载回来核对
```

**`download_url` 只在本次调用期间有效**，所以必须同步取字节；"存 URL 进任务、让节点稍后再拉"
必然过期。这条通路的下载安全按 SSRF 处理：只允许 https、DNS 解析后拒绝内网/回环/保留地址、
**每一跳重定向重新校验**（禁用 urllib 的自动跟随，否则一个 302 就能绕过拦截）、边读边计数超限即断。

## 设计原则：每个选择挡的是哪种失败

机制在论文第 3 节，这里写的是**为什么**。同样的机制可以为了不同理由装到一起，理由决定了边界上会发生什么。

**状态必须比产出它的进程活得久。** 长任务系统里有两个必死的进程：上下文窗口（会话结束就没了）和执行体
（机器重启、CLI 崩溃、父进程被杀）。主流做法是把第一个做大 —— 更长的 transcript、摘要、跨轮检索 ——
把上下文当成存储用。它不是存储：没有淘汰策略、没有 schema、没有查询语言、不能共享、没有审计痕迹，
而且它恰恰最可能在任务变长的那一刻被截断。我们把每个持久事实放在平均无故障时间最长的层：一个进程、
一个 SQLite、只追加。脑和执行体都被当成无状态客户端，随时可以丢弃、从账本重建。

**接口必须比能力窄。** harness 靠加工具长大。每个工具局部合理、全局昂贵：要记的更多、攻击面更大、
工具之间相互作用会生出新的失败模式。九个不是为优雅挑的预算，而是能通过"这是脑需要，还是执行体本来
就有"这个问题的那些。一切作用于世界的操作 —— shell、文件系统、浏览器、网络、凭据 —— 都属于执行体，
它本来就有台真机器。脑只拿到作用于**工作**的操作。

**按 what 路由，永不按 where。** 一旦派发指令里写了机器名，三件事同时成立：派发方必须了解舰队、
舰队不能再变动、审计记录的是机器而不是意图。按能力路由要求 hub 知道 **what**、永不知道 **where**。

**边缘比中心更了解自己的环境。** 指定步骤的派发，是派发方在声称自己了解执行环境。这通常是假的：
CLI 要不要 `--skip-git-repo-check`、仓库干不干净、cookie 过没过期、磁盘够不够 —— 全都只有边缘看得见，
而且从派发到执行之间还会变。

**证据是产物，不是断言。** 用退出码当完成信号，在"命令即目标"时够用，在"产出一份经过核实的本周
国内招生公告汇总"时毫无意义 —— 那件事没有退出码。而且执行命令的那个系统，恰恰是最没资格判断目标
是否达成的系统。

**失败是一等结果。** 大多数 harness 里失败是异常：循环中断、打印堆栈、人来读。长任务里这个形状是错的，
因为尾部任务里失败是常态。跑了四小时发现目标按原样无法达成，它产出了价值 —— 产出了一个结论 ——
把它当错误丢掉，就是把结论丢掉了。

**刻意没做的东西**（完整理由见论文附录 A）：hub 里不放 planner；不加消息总线或向量库；不允许任务中途
对话；不做多租户；路由路径里不放模型。

## 设计里几个刻意的选择

**按 capability 派活，不按机器。** 模型说"我要 `build.heavy` 这件事被完成"，而不是
"给 gpu-1 发命令"。谁有这个能力谁去领。任务结构里**没有** `machine_id`、没有 `command`、
没有 `shell` —— 永不加。

**goal 是目标，不是步骤。** 给模型和给执行体的是同一句话：要什么、边界在哪、怎么算完成。
执行体自己拆解、探索、验证；卡住了继续找替代路径，不回来问"接下来怎么办"。
真的做不完，就交一份完整的 `failed` 报告 —— 保留已完成的部分、写清卡在哪、证据在哪。

**取消必须传到执行面。** `close_task` 要是只改 PAD 里的状态，就会出现「控制面认为任务死了、
执行面还在跑」—— 占着执行槽，后面的任务接不进来。这条是真事故换来的：节点在执行期间轮询状态，
看到 `cancelled` 就杀掉整个进程组，且不再补交迟到的 `done`。

**超时不等于取消。** 会话软超时后 descendant 可能还在干有效活，走 lease/harvest 继续等；
只有显式 `cancelled` 才杀进程。

**报告要能自证。** `done` / `failed` 都带逐条验收证据，服务端会算覆盖度
（`_acceptance_check.missing` 告诉你哪几条没被回答）。它只判"有没有回答"、不判真伪 ——
真伪靠另一个节点独立复核。原始数据留本地只带统计，但上层点名要的字段一个字不许省。

**骨架会过期，所以有指纹。** 文档和提示词改在服务端，节点是拷贝，不会自动更新。
`bundle.py` 对行为代码和提示词取指纹，节点注册时报上来，服务端一比就知道谁在跑旧代码。

## 一个 PAD，三种脑：work agent 和 personal agent 是同一个槽位

"agent" 这个词被用在两种完全不同的东西上，混淆它们，是个人助理类产品大多做歪的原因。

- **work agent** —— 授权有边界。你说清楚要什么，它在划定的范围内做完。
- **personal agent** —— 授权是常态的。它自己决定做什么、什么时候做、用哪些资源。

行业默认这两者要两套系统。PAD 的答案是：**这只是"谁坐在 brain 槽位上"的区别，不是架构的区别。**
槽位是可替换的，hub 对此完全无感。

| 谁坐槽位 | 授权方式 | 入口 |
|---|---|---|
| 聊天模型 | 转授：你用自然语言提要求，模型自己决定发几个任务、挑哪些能力 | MCP `/mcp`，`created_by=chatgpt` |
| 你本人 | 直接：你在命令行点名能力和目标 | `padctl submit`，`created_by=human` |
| 定时器 | 周期：每天同一时间发同一条流水线 | 内部 timer，`created_by=daily-auto-timer` |

三者走**同一个** `submit_task`、写**同一张** `tasks` 表、过**同一套**验收闸门、出现在**同一份** digest 里。
下游没有任何一处按 `created_by` 分叉。这就是"模型无状态"真正的意思：**brain 不是一个组件，是一个角色**，
任何满足协议的东西都能坐上去。

生产环境跑了两个月，同一个 hub 上三种脑的真实分布：

| | 聊天模型 | 你本人 | 定时器 |
|---|---|---|---|
| 任务数 | 854 | 39 | 36 |
| 完成 | 680 | 36 | 19 |
| 失败 | 111 | 2 | 17 |
| 证据覆盖 | 96% | 47% | 100% |
| 中位时长 | 14.6 分钟 | 0.7 分钟 | 341 分钟 |
| 机时 | 632.9 | 6.9 | 194.4 |
| goal 中位长度 | 301 字 | 131 字 | 611 字 |
| 带验收条件 | 98% | 79% | 100% |
| 触及能力数 | 12 | 8 | 1 |

这张表是这套设计最硬的证据。**三个方向同时分得开**：

- 交给模型的活是**被规格化的**：goal 长 301 字，98% 带验收条件，散在 12 个能力上。
- 你亲手发的活是**被定向的**：goal 131 字、79% 带条件、中位不到 1 分钟、两个月总共 6.9 机时。
- 定时器在另一个极端：只有 1 个能力、goal 最长、**失败率 47%** —— 无人值守的固定流水线，
  没有人会发现它的目标已经漂移了。

**切换零成本、不需要迁移。** 46 个活跃日里，有 10 天同时出现聊天模型和你本人发的任务。
熟练、结果是一条命令的活，你自己发；需要调研、拆解、连续多次下发，交给模型。
两条路留下同一份审计轨迹，所以模式换掉，记录不会断成两截。

**授权是多大的差别，不是两套系统。** 谨慎的 work agent 和放手的 personal agent 之间，
差的不是沙箱、不是工具集，而是**有多少边界在事前被钉死**。`padctl` 一次交互把能力、目标、
验收条件全部写死，每个字段由人决定。聊天模型手上是同样的 9 个工具、同样的天花板，
但它会连发多个任务、自己挑能力 —— 所以它的 goal 长度是你亲手发的两倍多（301 vs 131 字），
委派越多，规格写得越细。

**要给它更多自主权，是改 YAML，不是改架构。** personal assistant 模式 = PAD + 一个高频提交、
长会话、手里握着一大把能力的客户端。hub 一行都不用改，改的是**你声明了哪些能力**。
把授权决定留在这个位置是有意的：它保持显式、可撤销，而不是埋进 agent 的 prompt 里。

**但自主权有代价，数据说得很清楚。** 让切换零成本的同一个机制，也让过度委派变得容易：
一个拿着宽能力清单的模型，会很乐意提交一个没有任何能力满足得了的目标，hub 老老实实记一笔失败。
失败分类里约一半是这种形状。**如果当初把路由权交给模型，这套系统根本产不出失败分类** ——
因为它没有一个地方统一记录哪里错了。

## 多机协调，但没有协调者

对 hub 最自然的质疑是"中心化等于瓶颈"。PAD **状态集中、执行分散**，这个区分才是六台机器
不需要一个调度进程就能协作的原因。

hub 里没有任何队列 worker，它自己不执行任何任务。每次轮询它只回答两个问题：
**有我声明过的能力对应的活吗**、**我手上这些活是什么状态**。每个节点按自己的节奏轮询、
有空槽才领。吞吐是各节点本地决策之和，不是某个全局调度器的产出。

**系统里根本没有"分配"这一步。** 任务是**被领走的**，不是被派下去的。hub 的路由表是一堆声明，
不是一堆决策。四个性质随之而来，每一个都省掉传统编排器需要的一台机器：

- **没有调度器，没有一致性协议。** 活从一张按能力索引的表里领，hub 不需要全局负载视图，
  也不需要就"谁该干"达成一致。加机器是注册，减机器是超时。
- **背压是自动的。** 声明 `concurrency: N` 的节点不会去领第 N+1 个。队列长在数据库里 ——
  这才是一个可见的位置，而不是长在 broker 的内存里。
- **失败是局部的。** 节点中途死掉，任务留在 `running`、lease 到期；报告是追加式且幂等的，
  下一个拿到这个能力的节点从账本接着做，不用从某个进程的残骸里恢复。
- **协调记录就是审计记录。** 每次领取、每份报告、每个产物都是一行。失败分类不需要额外埋点，
  原因就在这里：调度器为自己记录的数据，和审计需要的数据是同一份。

代价是真的，说在明处：一个 hub 就是单点协调，重启会丢掉在途任务的进程监管。
单操作者、几台机器，这笔交易明显划算；多租户场景不划算，PAD 也不做这个声称。

## 目录

| 文件 | 作用 |
|---|---|
| `server.py` | 服务端（FastAPI + MCPServer + SQLite），两扇门都在这 |
| `db.py` / `schema.sql` | 状态层 |
| `ld_agent.py` | 每台机器跑的节点守护进程 |
| `harness/run_harness.py` | 把任务翻译成 opencode / claude / codex 的无头调用 |
| `harness/run_script.py` | 把一条命令收敛成 PAD 契约（确定性活用它） |
| `harness/prompt.md.tmpl` | 执行体提示词。改规矩只动这里 |
| `padctl.py` | 人用的 CLI（也有 `--file`） |
| `pad_mcp.py` | 走 MCP 端点的调试工具（和大脑同一条路） |
| `quickstart.sh` | 一键起步 |
| `contract.md` | 契约：下发基准、汇报格式、执行纪律 |
| `JOIN.md` | 新机器接入协议 + 自查清单 + 排坑表 |
| `CHANGELOG.md` | 改完之后行为不一样的事，排查先看这里 |
| `docs/make_preview.py` | 重新生成首屏动图（真实输出 → 逐帧截图 → ffmpeg） |
| `paper/` | 系统报告（ICML Template，`pad.pdf`）+ 只含聚合的实测数据 |
| `tools/collect_stats.py` | 从线上 PAD 抓任务详情，复现论文里的数字 |
| `README.zh.md` | 中文版（默认展示的是英文 `README.md`） |

## 开发

```bash
.venv/bin/python -m unittest discover -s tests -v   # 单测
bash tests/e2e_input_files.sh                       # 端到端：起临时服务端+节点，走真链路
```

演示图不是手拼的，是捕获本机真实输出再截图。改了输出格式就重跑：

```bash
./quickstart.sh          # 先有一个跑着的本地 PAD
.venv/bin/python docs/make_preview.py
```

## 论文

`paper/pad.pdf`（ICML Template，正文 8 页 + 附录 5 页）把上面这些设计写成了可引用的系统报告：
问题定义、与 harness / long-horizon / delegation 三支工作的关系、架构、设计原则、以及实测。
附录逐项展开数据库 schema、任务状态机、九个工具的契约、以及复现脚本。
仓库里只发布成品 PDF，LaTeX 源码本地保留。

实测部分用的是这套系统在生产环境跑了两个月之后的真实日志：

| 指标 | 数值 |
|---|---|
| 节点 / 能力 | 6 台机器、20 个能力、3 个厂商的 agent CLI |
| 真实任务（不含自检） | 938 个，835 机时 |
| 长任务占比 | 18.7% 超过 1 小时，25 个超过 6 小时，最长 42 小时 |
| 完成任务的逐条验收证据 | 93% |
| 失败任务仍交结构化报告 | 42% |
| 失败主因 | 64/132 是执行体环境问题（不是模型不会做） |

最后一行是整套设计最想说明的事：**大部分失败发生在环境里，不在推理里**。单环 harness 看到的是
"工具调用挂了"，PAD 看到的是"这台机器的这个能力一直挂"，后者才是能修的那个。

数据与复现脚本在 `paper/data/aggregates.json` 和 `tools/collect_stats.py`。
原始抓取含 operator 的真实 goal 文本，不进仓库。

## 已知缺口

- 认证是 URL 里的密钥，没有 OAuth。换 OAuth 只需换反代前面那层，契约不动
- `running` 中途崩溃（没交最终报告）的任务不会被自动接管，需要一个 stale reaper
- 单文件 10 MiB 上限来自"服务端内存里整块收"；要发几百 MB 得分片落盘
- 非图片输入不在 PAD 内解析（不做 PDF 抽文/Office 转换），交给节点执行体用本机工具读
- ChatGPT Business/Enterprise 的 workspace 发布路径没做，只做了个人 developer mode

---

MIT License
