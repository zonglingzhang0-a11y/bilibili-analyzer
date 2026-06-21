"""
Markdown 报告生成模块
图表生成 + Markdown 模板 + 原始数据附录
"""
import os
import json
import matplotlib
matplotlib.use("Agg")  # 无 GUI 后端
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
from matplotlib.ticker import MaxNLocator

# ── 中文字体配置 ──
def _setup_chinese_font():
    """配置 matplotlib 中文显示"""
    font_paths = [
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/msyh.ttc",
    ]
    for fp in font_paths:
        if os.path.exists(fp):
            fm.fontManager.addfont(fp)
            prop = fm.FontProperties(fname=fp)
            font_name = prop.get_name()
            matplotlib.rcParams["font.family"] = font_name
            matplotlib.rcParams["axes.unicode_minus"] = False
            return font_name
    return None

_FONT_NAME = _setup_chinese_font()

# 统一配色
COLORS = {
    "primary": "#00A1D6",      # B站蓝
    "secondary": "#FB7299",    # B站粉
    "positive": "#4CAF50",
    "neutral": "#FF9800",
    "negative": "#F44336",
    "scroll": "#00A1D6",
    "top": "#FB7299",
    "bottom": "#9C27B0",
    "reverse": "#607D8B",
    "special": "#FF5722",
}

# 图表保存 DPI
CHART_DPI = 150


# ═══════════════════════════════════════════════════════
# 图表函数
# ═══════════════════════════════════════════════════════

def chart_danmaku_density(heatmap: dict, output_path: str, title: str = "弹幕密度曲线"):
    """弹幕密度时间曲线（折线图）

    Args:
        heatmap: {"0:00": 15, "0:10": 22, ...}
        output_path: PNG 输出路径
    """
    if not heatmap:
        return None

    times = list(heatmap.keys())
    values = list(heatmap.values())
    x = list(range(len(times)))

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.fill_between(x, values, alpha=0.2, color=COLORS["primary"])
    ax.plot(x, values, color=COLORS["primary"], linewidth=1.2)

    # x 轴标签稀疏化
    step = max(1, len(times) // 15)
    tick_positions = x[::step]
    tick_labels = times[::step]
    ax.set_xticks(tick_positions)
    ax.set_xticklabels(tick_labels, rotation=45, fontsize=7)

    ax.set_ylabel("弹幕数量（每10秒）", fontsize=10)
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_xlim(0, len(x) - 1)
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))

    fig.tight_layout()
    fig.savefig(output_path, dpi=CHART_DPI)
    plt.close(fig)
    return output_path


def chart_sentiment_pie(sentiment: dict, output_path: str, title: str = "评论情感分布"):
    """情感分布饼图

    Args:
        sentiment: {"positive": 45, "neutral": 30, "negative": 5}
    """
    if not sentiment:
        return None

    labels = ["正面", "中立", "负面"]
    sizes = [
        sentiment.get("positive", 0),
        sentiment.get("neutral", 0),
        sentiment.get("negative", 0),
    ]
    colors = [COLORS["positive"], COLORS["neutral"], COLORS["negative"]]
    explode = (0.02, 0, 0)

    total = sum(sizes)
    if total == 0:
        return None

    fig, ax = plt.subplots(figsize=(5, 5))
    wedges, texts, autotexts = ax.pie(
        sizes, explode=explode, labels=labels, colors=colors,
        autopct=lambda pct: f"{pct:.1f}%\n({int(pct/100*total)}条)",
        startangle=90, pctdistance=0.6,
    )
    for t in autotexts:
        t.set_fontsize(9)
    ax.set_title(title, fontsize=12, fontweight="bold")

    fig.tight_layout()
    fig.savefig(output_path, dpi=CHART_DPI)
    plt.close(fig)
    return output_path


def chart_sentiment_curve(curve: list[dict], output_path: str, title: str = "弹幕情感时间曲线"):
    """弹幕情感时间曲线（堆叠面积图）

    Args:
        curve: [{"time": "0:00", "positive": 5, "neutral": 3, "negative": 1}, ...]
    """
    if not curve:
        return None

    times = [c["time"] for c in curve]
    positive = [c["positive"] for c in curve]
    neutral = [c["neutral"] for c in curve]
    negative = [c["negative"] for c in curve]
    x = list(range(len(times)))

    fig, ax = plt.subplots(figsize=(12, 4))
    ax.stackplot(x, positive, neutral, negative,
                 labels=["正面", "中立", "负面"],
                 colors=[COLORS["positive"], COLORS["neutral"], COLORS["negative"]],
                 alpha=0.8)

    step = max(1, len(times) // 15)
    ax.set_xticks(x[::step])
    ax.set_xticklabels(times[::step], rotation=45, fontsize=7)
    ax.set_ylabel("弹幕数量", fontsize=10)
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.legend(loc="upper right", fontsize=8)

    fig.tight_layout()
    fig.savefig(output_path, dpi=CHART_DPI)
    plt.close(fig)
    return output_path


def chart_top_words(words: list[tuple], output_path: str, title: str = "高频词"):
    """高频词横向条形图

    Args:
        words: [("词1", 42), ("词2", 38), ...]
    """
    if not words:
        return None

    top = words[:15]
    labels = [w[0] for w in top][::-1]
    values = [w[1] for w in top][::-1]

    fig, ax = plt.subplots(figsize=(8, 5))
    bars = ax.barh(labels, values, color=COLORS["primary"], height=0.6)
    for bar, v in zip(bars, values):
        ax.text(bar.get_width() + max(values)*0.01, bar.get_y() + bar.get_height()/2,
                str(v), va="center", fontsize=9)

    ax.set_xlabel("出现次数", fontsize=10)
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_xlim(0, max(values) * 1.15)

    fig.tight_layout()
    fig.savefig(output_path, dpi=CHART_DPI)
    plt.close(fig)
    return output_path


def chart_danmaku_modes(modes: dict, output_path: str, title: str = "弹幕类型分布"):
    """弹幕类型分布饼图

    Args:
        modes: {"滚动": 664, "顶部": 766, "底部": 2}
    """
    if not modes:
        return None

    mode_colors = {
        "滚动": COLORS["scroll"], "顶部": COLORS["top"],
        "底部": COLORS["bottom"], "逆向": COLORS["reverse"],
        "高级": COLORS["special"],
    }
    labels = list(modes.keys())
    sizes = list(modes.values())
    colors = [mode_colors.get(k, "#999999") for k in labels]

    total = sum(sizes)
    if total == 0:
        return None

    fig, ax = plt.subplots(figsize=(5, 5))
    wedges, texts, autotexts = ax.pie(
        sizes, labels=labels, colors=colors,
        autopct=lambda pct: f"{pct:.1f}%",
        startangle=90, pctdistance=0.6,
    )
    for t in autotexts:
        t.set_fontsize(9)
    ax.set_title(title, fontsize=12, fontweight="bold")

    fig.tight_layout()
    fig.savefig(output_path, dpi=CHART_DPI)
    plt.close(fig)
    return output_path


def chart_cross_video_comparison(
    overall: list[dict], rankings: dict, output_path: str, title: str = "跨视频综合评分"
):
    """跨视频综合评分柱状图

    Args:
        overall: [{"rank": 1, "title": "...", "overall_score": 85.5}, ...]
        rankings: 各维度排名数据
    """
    if not overall:
        return None

    top = overall[:8]
    titles = [f"#{v['rank']} {v['title'][:18]}" for v in top][::-1]
    scores = [v["overall_score"] for v in top][::-1]

    fig, ax = plt.subplots(figsize=(10, 5))
    bars = ax.barh(titles, scores, color=COLORS["primary"], height=0.5)
    for bar, s in zip(bars, scores):
        ax.text(bar.get_width() + 0.5, bar.get_y() + bar.get_height()/2,
                str(s), va="center", fontsize=9)

    ax.set_xlabel("综合评分", fontsize=10)
    ax.set_title(title, fontsize=12, fontweight="bold")
    ax.set_xlim(0, max(scores) * 1.1 + 5)

    fig.tight_layout()
    fig.savefig(output_path, dpi=CHART_DPI)
    plt.close(fig)
    return output_path


# ═══════════════════════════════════════════════════════
# Markdown 报告生成
# ═══════════════════════════════════════════════════════

def _md_section(title: str, level: int = 2) -> str:
    """Markdown 标题"""
    return f"\n{'#' * level} {title}\n"


def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Markdown 表格"""
    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines) + "\n"


def _md_img(src: str, alt: str = "", relative_to: str = None) -> str:
    """Markdown 图片引用"""
    if relative_to:
        src = os.path.relpath(src, relative_to) if os.path.isabs(src) else src
    return f"![{alt}]({src})\n"


def _md_collapse(summary: str, content: str) -> str:
    """折叠块"""
    return f"<details>\n<summary>{summary}</summary>\n\n{content}\n</details>\n"


def generate_video_report(
    stats: dict,
    output_dir: str,
    video_title: str = "",
    content_prompts: dict = None,
) -> str:
    """生成单个视频的 Markdown 报告

    Args:
        stats: build_statistics 的输出
        output_dir: 输出目录（图表和 report.md 保存到这）
        video_title: 视频标题
        content_prompts: MCP 分析提示词（可选）

    Returns:
        report.md 文件路径
    """
    charts_dir = os.path.join(output_dir, "charts")
    os.makedirs(charts_dir, exist_ok=True)

    lines = []
    title = f"# {video_title} — B站视频深度分析报告\n" if video_title else "# B站视频深度分析报告\n"
    lines.append(title)

    # ── 1. 视频概览 ──
    lines.append(_md_section("1. 视频概览"))
    vs = stats.get("video_specs", {})
    if vs:
        pages_count = vs.get("pages_count", 1)
        pages_str = f"（{pages_count}分P）" if pages_count > 1 else ""
        dur = vs.get("duration_seconds", 0)
        dur_str = f"{dur // 60}分{dur % 60}秒"
        desc = vs.get("desc", "").replace("\n", " ")[:120] if vs.get("desc") else "-"
        lines.append(_md_table(
            ["属性", "值"],
            [
                ["分辨率", f"{vs.get('width', '?')} × {vs.get('height', '?')}"],
                ["时长", f"{dur_str} {pages_str}"],
                ["简介", desc],
            ],
        ))

    # 封面
    cover_path = stats.get("cover_path", "")
    if cover_path and os.path.exists(cover_path):
        lines.append(_md_img(cover_path, "视频封面"))
        # MCP 封面分析结果（优先从 stats 中读取 mcp_analysis，回退到独立文件）
        cover_analysis_text = None
        mcp_cover = stats.get("mcp_analysis", {}).get("cover", "")
        if mcp_cover:
            cover_analysis_text = mcp_cover
        else:
            cover_analysis_path = os.path.join(output_dir, "cover_analysis.json")
            if os.path.exists(cover_analysis_path):
                try:
                    with open(cover_analysis_path, "r", encoding="utf-8") as f:
                        ca = json.load(f)
                    cover_analysis_text = ca.get('description', ca.get('analysis', ''))
                except Exception:
                    pass
        if cover_analysis_text:
            lines.append(f"**封面分析：** {cover_analysis_text}\n")

    # ── 2. 弹幕分析 ──
    lines.append(_md_section("2. 弹幕分析"))
    dm = stats.get("danmaku", {})
    if dm:
        lines.append(f"- 弹幕总数：**{dm['total']}** 条")
        lines.append(f"- 弹幕密度：**{dm['density_per_minute']}** 条/分钟\n")

        # 弹幕类型分布图
        modes = dm.get("mode_distribution", {})
        if modes:
            chart_path = os.path.join(charts_dir, "danmaku_modes.png")
            chart_danmaku_modes(modes, chart_path)
            if os.path.exists(chart_path):
                lines.append(_md_img(chart_path, "弹幕类型分布"))

        # 弹幕密度曲线
        heatmap = dm.get("time_heatmap", {})
        if heatmap:
            chart_path = os.path.join(charts_dir, "danmaku_density.png")
            chart_danmaku_density(heatmap, chart_path, f"《{video_title}》弹幕密度曲线")
            if os.path.exists(chart_path):
                lines.append(_md_img(chart_path, "弹幕密度曲线"))

        # 热门颜色
        top_colors = dm.get("top_colors", [])[:5]
        if top_colors:
            lines.append("**热门弹幕颜色：**")
            color_str = " ".join(f"`{c}`" for c, n in top_colors)
            lines.append(color_str + "\n")

    # 弹幕情感曲线
    curve = stats.get("sentiment_curve", [])
    if curve:
        lines.append(_md_section("弹幕情感曲线", level=3))
        chart_path = os.path.join(charts_dir, "sentiment_curve.png")
        chart_sentiment_curve(curve, chart_path, f"《{video_title}》弹幕情感时间曲线")
        if os.path.exists(chart_path):
            lines.append(_md_img(chart_path, "弹幕情感曲线"))

    # 弹幕高潮时刻
    peaks = stats.get("danmaku_peaks", [])
    if peaks:
        lines.append(_md_section("弹幕高潮时刻", level=3))
        for i, p in enumerate(peaks, 1):
            samples = " | ".join(p.get("sample", [])[:4])
            lines.append(f"**#{i}  {p['time']}** — {p['density']} 条/6秒")
            lines.append(f"> 弹幕样本：{samples}\n")

    # ── 3. 评论分析 ──
    lines.append(_md_section("3. 评论分析"))
    c = stats.get("comments", {})
    if c:
        lines.append(f"- 主评论数：**{c['total']}** | 子回复数：**{c['total_replies']}**")
        lines.append(f"- 平均点赞：**{c['avg_likes']}** | UP主回复率：**{c.get('up_reply_rate', 'N/A')}**\n")

        sentiment = c.get("sentiment", {})
        if sentiment:
            chart_path = os.path.join(charts_dir, "sentiment_pie.png")
            chart_sentiment_pie(sentiment, chart_path)
            if os.path.exists(chart_path):
                lines.append(_md_img(chart_path, "评论情感分布"))

        top_words = c.get("top_words", [])
        if top_words:
            chart_path = os.path.join(charts_dir, "top_words_comments.png")
            chart_top_words(top_words, chart_path, f"《{video_title}》评论高频词")
            if os.path.exists(chart_path):
                lines.append(_md_img(chart_path, "评论高频词"))

        # 最长评论
        longest = c.get("longest_comment")
        if longest:
            lines.append(f"**最长评论（{longest['length']}字，{longest['likes']}赞）：**")
            lines.append(f"> {longest['content'][:200]}\n")

    # ── 4. 评论 vs 弹幕主题差异 ──
    div = stats.get("divergence", {})
    if div:
        lines.append(_md_section("4. 评论 vs 弹幕 主题差异"))
        cuw = div.get("comment_unique_words", [])
        duw = div.get("danmaku_unique_words", [])
        sw = div.get("shared_words", [])
        if cuw:
            lines.append(f"**评论特有词：** {', '.join(w for w, _ in cuw[:10])}")
        if duw:
            lines.append(f"**弹幕特有词：** {', '.join(w for w, _ in duw[:10])}")
        if sw:
            lines.append(f"**共同高频词：** {', '.join(w for w, _ in sw[:10])}")
        lines.append("")

    # ── 5. 高潮帧画面分析 ──
    frame_results = stats.get("frame_results", [])
    mcp_frames = stats.get("mcp_analysis", {}).get("frames", [])
    if frame_results:
        lines.append(_md_section("5. 高潮帧画面分析"))
        # 尝试读取旧的 MCP 分析结果（frame_analysis.json，向后兼容）
        frame_analysis_path = os.path.join(output_dir, "frame_analysis.json")
        frame_analysis = {}
        if os.path.exists(frame_analysis_path):
            try:
                with open(frame_analysis_path, "r", encoding="utf-8") as f:
                    frame_analysis = json.load(f)
            except Exception:
                pass

        for i, fr in enumerate(frame_results, 1):
            ti = fr.get("timestamp_info", {})
            time_str = ti.get("time", "?")
            density = ti.get("density", 0)
            lines.append(f"### 高潮 #{i} — {time_str}（{density}条/6秒）\n")
            frame_path = fr.get("frame_path", "")
            if frame_path and os.path.exists(frame_path):
                lines.append(_md_img(frame_path, f"高潮帧 {time_str}"))

            # MCP 分析结果：优先从 stats.mcp_analysis.frames 读取（新格式）
            fa_data = None
            # 新格式：mcp_frames 是列表，每个元素有 time 字段
            for mf in mcp_frames:
                if mf.get("time") == time_str:
                    fa_data = mf
                    break
            # 旧格式：frame_analysis.json 字典，key 为时间
            if not fa_data:
                fa_key = ti.get("time", "")
                fa_data = frame_analysis.get(fa_key, {})
            # 尝试读取独立 per-frame 分析文件
            if not fa_data:
                safe_time = time_str.replace(":", "m", 1).replace(":", "s", 1)
                per_frame_path = os.path.join(output_dir, f"frame_{safe_time}_analysis.json")
                if os.path.exists(per_frame_path):
                    try:
                        with open(per_frame_path, "r", encoding="utf-8") as f:
                            fa_data = json.load(f)
                    except Exception:
                        pass

            if fa_data:
                analysis_text = fa_data.get('description', fa_data.get('analysis', ''))
                if analysis_text:
                    lines.append(f"**画面描述：** {analysis_text}")
                why_peaked = fa_data.get('why_peaked', '')
                if why_peaked:
                    lines.append(f"**引爆弹幕原因：** {why_peaked}")
                if analysis_text or why_peaked:
                    lines.append("")
            else:
                # 显示弹幕样本作为参考
                samples = " | ".join(ti.get("sample", [])[:4])
                lines.append(f"*弹幕样本：{samples}*\n")

    # ── 附录：原始数据 ──
    lines.append(_md_section("附录：原始数据"))
    lines.append(f"完整原始数据请查看附件：`stats.json`\n")
    # 同时保存一份独立 stats.json（供查阅和程序读取）
    stats_file = os.path.join(output_dir, "stats.json")
    with open(stats_file, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    # 写出报告
    report = "\n".join(lines)
    report_path = os.path.join(output_dir, "report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    return report_path


def generate_summary_report(
    all_stats: list[dict],
    output_dir: str,
    series_info: dict = None,
) -> str:
    """生成多视频汇总 Markdown 报告

    Args:
        all_stats: [{"aid", "title", "views", "likes", "stats"}, ...]
        output_dir: 输出目录
        series_info: 周热榜期号信息 {"number": 377, "name": "..."}

    Returns:
        report.md 文件路径
    """
    charts_dir = os.path.join(output_dir, "charts")
    os.makedirs(charts_dir, exist_ok=True)

    lines = []
    if series_info:
        lines.append(f"# 每周必看 第{series_info['number']}期 — {series_info.get('name', '')} 分析报告\n")
    else:
        lines.append("# B站视频横向对比分析报告\n")

    lines.append(f"共分析 **{len(all_stats)}** 个视频\n")

    # 构建跨视频对比
    from stats import build_cross_video_comparison
    comparison = build_cross_video_comparison(all_stats)

    # 综合评分图
    overall = comparison.get("overall_scores", [])
    if overall:
        lines.append(_md_section("综合评分排行"))
        chart_path = os.path.join(charts_dir, "cross_video_score.png")
        chart_cross_video_comparison(overall, comparison.get("rankings", {}), chart_path)
        if os.path.exists(chart_path):
            lines.append(_md_img(chart_path, "综合评分排行"))
        lines.append("")

    # 各维度排行表格
    rankings = comparison.get("rankings", {})
    dim_labels = {
        "danmaku_density": ("📊 弹幕密度排行", "条/分钟"),
        "up_interaction": ("💬 UP主互动率排行", "%"),
        "positive_sentiment": ("😊 正面评论率排行", "%"),
        "comment_depth": ("📝 评论质量排行", "赞"),
    }
    for dim_key, (label, unit) in dim_labels.items():
        items = rankings.get(dim_key, [])
        if not items:
            continue
        lines.append(_md_section(label))
        rows = [[str(it["rank"]), it["title"][:40], f"{it['value']} {unit}"]
                for it in items]
        lines.append(_md_table(["排名", "视频", "数值"], rows))

    # 视频基本信息汇总表
    lines.append(_md_section("视频基本信息"))
    summary_rows = []
    for item in sorted(all_stats, key=lambda x: -x.get("views", 0)):
        s = item.get("stats", {})
        c = s.get("comments", {})
        d = s.get("danmaku", {})
        summary_rows.append([
            item["title"][:35],
            f"{item.get('views', 0):,}",
            f"{c.get('total', 0)}",
            f"{d.get('total', 0)}",
            f"{d.get('density_per_minute', 0)}",
        ])
    lines.append(_md_table(
        ["视频", "播放量", "评论数", "弹幕数", "弹幕密度/分钟"],
        summary_rows,
    ))

    # 附录
    lines.append(_md_section("附录：原始数据"))
    lines.append(f"完整原始数据请查看附件：`summary.json`\n")

    report = "\n".join(lines)
    report_path = os.path.join(output_dir, "report.md")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    return report_path
