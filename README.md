# B站每周必看分析器

采集 B站「每周必看」榜单（或指定视频）的评论与弹幕，生成统计数据和 Markdown 报告：

- **评论**：双排序（时间 + 热度）采集去重、情感分布、高频词、UP 主回复率
- **弹幕**：密度热力图、类型/颜色分布、情感时间曲线、高潮时刻检测（支持多分P）
- **内容**：视频参数、封面、弹幕高潮时刻的画面截图，并生成供多模态模型分析的提示词
- **汇总**：同一期视频的横向排行与综合评分

## 安装

需要 Python 3.10+。

```bash
pip install -r requirements.txt
```

## 登录凭证（必需）

未登录时 B站每个视频每种排序只返回约 3 条评论，评论数据会严重不全。任选一种方式配置 `SESSDATA`：

- 环境变量 `BILI_SESSDATA=<你的 SESSDATA>`
- 或在项目根目录创建 `.sessdata` 文件，写入 SESSDATA 的值（已在 `.gitignore` 中忽略）

获取方式：浏览器登录 B站 → 开发者工具 → Cookie → 复制 `SESSDATA`。SESSDATA 会过期，程序启动时会检查登录状态并提示。自检：

```bash
python bili_auth.py
```

## 用法

```bash
# 最新一期，全部视频
python main.py

# 指定期号，只处理前 5 个视频，评论每种排序最多 10 页（每页 20 条）
python main.py -s 377 -n 5 -l 10

# 列出最近的期号
python main.py --list

# 单个视频（cid、时长会自动获取）
python main.py --aid 47126553
```

| 参数 | 说明 |
| --- | --- |
| `-s, --series` | 期号，不指定则为最新一期 |
| `-n, --number` | 最多处理的视频数量（默认全部） |
| `-l, --limit` | 评论每种排序最多采集的页数（默认 100） |
| `-o, --output` | 输出根目录（默认 `./bilibili_output`） |
| `--aid / --cid / --duration / --title` | 直接分析指定视频 |
| `--no-content` | 跳过内容分析（视频参数、封面、高潮截图） |
| `--no-frames` | 保留封面，不截取高潮画面 |
| `--no-checkpoint` | 不做断点续传，每次新建目录 |
| `--no-adaptive` | 关闭自适应限流，使用固定延迟 |
| `--no-maximize-comments` | 只按时间排序采集评论 |
| `--full-comments` | 全量采集一级评论：按时间排序翻到最后一页（忽略 `-l`），耗时很长 |
| `--comments-only` | 只重新采集最近一次运行中已完成视频的评论（支持中断后继续） |
| `--rebuild [RUN_DIR]` | 离线重建：用已保存的评论/弹幕重新统计并生成报告，不联网 |
| `--log-file PATH` | 输出同时追加写入日志文件（UTF-8），适合长时间运行 |
| `--run-name NAME` | 指定运行目录名（如 `20260911_180000`）；目录已存在时接着其中的进度继续；配合 `--comments-only` 表示重采该目录 |

### 断点续传与失败重试

同一期的采集中断后再次运行，会自动复用该期未完成的目录并跳过已完成的视频。评论或弹幕采集失败的视频会在本次运行末尾补采；仍然失败的记为 `failed`，下次运行时重新采集。

### 评论采集量

默认每个视频按「最新 + 最热」两种排序各采最多 100 页（每页 20 条）并去重。样本是最新和最热的评论，不是随机抽样；想要完整数据时使用全量模式：

```bash
python main.py --comments-only --full-comments   # 为最近一次运行的视频补采全部一级评论
```

- 接口给出的评论总数**包含楼中楼回复**，全量采到的一级评论通常只占总数的一部分；楼中楼只保存每条评论附带的少量预览回复。
- 实测每页约 2～3 秒，一期几十个视频全量采集需要数小时。每个视频采完即记录到运行目录的 `comments_recollect.json`，中断后重新运行同一命令会跳过已完成的视频。
- 耗时较长，建议在独立的终端窗口运行，并加上 `--log-file` 保存日志：`python main.py --comments-only --full-comments --log-file recollect.log`
- 网络出错（超时、SSL 断连等）时自动重试同一页，最多约 12 分钟，已采到的进度不丢。
- 某个视频最终失败时保留原有数据、不记为完成，冷却 5 分钟后继续下一个；连续 3 个视频失败会自动暂停，确认网络正常后重新运行同一命令即可继续。

### 进度看板

长时间运行时，可以在浏览器里实时查看进度（只读，不影响采集）：

```bash
python main.py -s 389 --full-comments --run-name 20260911_180000 --log-file bilibili_output/20260911_180000/run.log
python dashboard.py bilibili_output/20260911_180000          # 另开一个终端，然后打开 http://127.0.0.1:8765
```

看板显示已完成视频数、按评论量计的进度、当前视频所处阶段和翻页进度、网络重试/失败事件、预计剩余时间，以及每个视频的评论数和弹幕数。完整采集和 `--comments-only` 评论重采都支持。连续 3 个视频失败时程序会自动暂停。

### 离线重建

升级统计算法后，或需要修复旧版本报告的图片链接时：

```bash
python main.py --rebuild                                  # 重建 bilibili_output 下的全部运行
python main.py --rebuild bilibili_output/20260911_214129  # 只重建一次运行
```

只读取 `comments.json` / `danmaku.json`，会**重写** `stats.json`、`report.md`、`charts/` 以及汇总文件。旧版本数据中重复的评论会被去除；旧版本没有保存评论者 mid，UP 主回复率沿用原值。

## 输出结构

```
bilibili_output/
└── 20260911_214129/              # 一次运行
    ├── resume_state.json         # 断点续传清单
    ├── summary.json / report.md  # 跨视频对比与汇总报告
    ├── mcp_manifest.json         # 画面分析任务清单
    └── {aid}_{标题}/
        ├── comments.json / danmaku.json   # 原始数据
        ├── stats.json                     # 统计结果
        ├── report.md + charts/            # 报告与图表
        ├── cover.jpg + frames/            # 封面与高潮截图
        └── content_prompts.json           # 画面分析提示词
```

## 画面分析（可选）

采集完成后，封面和高潮截图可交给多模态模型分析，结果合并回报告：

```bash
python mcp_integrator.py --list  bilibili_output/<运行目录>   # 查看待分析任务
# 由 agent 按清单调用图像分析工具，把结果写入各任务的 result_path
python mcp_integrator.py --apply bilibili_output/<运行目录>   # 合并结果并重新生成报告
```

## 开发

```bash
pip install -r requirements-dev.txt
python -m pytest
```

测试不发网络请求。项目结构：

| 模块 | 职责 |
| --- | --- |
| `main.py` | 命令行入口、采集流程、断点续传与重试 |
| `ranking.py` | 每周必看期号与视频列表 |
| `comments.py` / `danmaku.py` | 评论、弹幕采集与 JSON 还原 |
| `content_analyzer.py` | 视频参数、封面、高潮截图、画面分析提示词 |
| `stats.py` | 统计分析（情感、词频、热力图、高潮检测、跨视频对比） |
| `report_writer.py` | 图表与 Markdown 报告 |
| `rebuild.py` | 离线重建 |
| `dashboard.py` | 采集进度看板 |
| `checkpoint_manager.py` / `adaptive_retry.py` | 断点续传清单、自适应限流与重试队列 |
| `wbi.py` / `bili_auth.py` / `bili_http.py` | WBI 签名、登录凭证、公共请求配置 |

## 已知限制

- 情感分析基于词典和否定词规则，适合看整体倾向，不适合逐条精确判断。
- 多分P视频需要视频参数（默认开启）才能逐P采集弹幕；使用 `--no-content` 时只采集主分P。
- 高潮截图来自 B站缩略图拼图，分辨率较低，时间点为近似位置。
