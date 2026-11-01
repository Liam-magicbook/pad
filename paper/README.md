# paper/

PAD 的系统报告，ICML 2025 排版。

```bash
cd paper
latexmk -pdf pad.tex     # → pad.pdf（9 页）
latexmk -c               # 清中间产物
```

`icml2025.sty` / `icml2025.bst` / `fancyhdr.sty` / `algorithm*.sty` 取自 ICML 2025
官方样式包（`media.icml.cc/Conferences/ICML2025/Styles/icml2025.zip`），未改动。

## 结构

| 节 | 内容 |
|---|---|
| 1–2 | 问题定位与相关工作：harness、long-horizon state、delegation、协议 |
| 3 | 架构：两扇门 / 能力路由 / goal 派发 / 证据闸门 / 执行体无关 |
| 4 | 运行模型：脑能做什么、汇报纪律、加能力 |
| 5 | 实测：6 台机器、938 个真实任务、835 机时 |
| 6 | 失败分类（132 个失败任务） |
| 7 | 讨论：三件事买到了什么、三件事是代价 |
| 附录 | 九个工具、复现方式 |

## 数据

`data/aggregates.json` 是**只含聚合**的数据集（计数、分位数、分类），可以公开。
原始抓取 `data/tasks.json` 含真实的 goal 文本和任务 id，属 operator 数据，已被
`.gitignore` 排除。

两个脚本：

```bash
# 1) 从线上 PAD 抓任务详情（需要 ~/.pad_key）
python3 tools/collect_stats.py --out paper/data/tasks.json

# 2) 压成只含聚合的论文数据集
python3 tools/make_paper_data.py paper/data/tasks.json paper/data/aggregates.json
```

失败分类用固定的一阶匹配规则（`tools/classify_failures.py`），同一份数据永远得到同一张表。

## 口径说明

- **work tasks = 全部任务 − 自检任务**（`*.ping.self`），因为自检的时长没有信息量。
- **machine-hours = 占用时长**（created → 终态，逐节点求和），包含排队；排队是操作者真实感受到的。
- **evidence coverage** 指终态报告逐条回答了验收条件；服务端只判"有没有回答"，不判真伪。
- 论文里同时给出`79% 有回答过验收` 和 `52% 没有留下未答项` 两个数字，因为它们量的是不同的事。

改论文里的数字时，请重跑上面两步，不要手改。
