"""
离线重建：用已保存的原始数据重新统计并生成报告（不发任何网络请求）

适用场景：
  - 统计算法升级后（如情感分析、弹幕热力图），刷新旧的分析结果
  - 修复旧版本报告中失效的图片链接

只读取 comments.json / danmaku.json，不修改它们；会重写 stats.json、report.md、charts/
以及运行目录下的 summary.json 和汇总 report.md。
"""
import json
import os
import re

from comments import comment_from_dict
from danmaku import danmaku_from_dict
from report_writer import generate_video_report, write_summary
from stats import build_statistics

# 无法从原始评论/弹幕重新算出、需要从旧 stats.json 沿用的字段
PRESERVED_KEYS = (
    "video_specs", "cover_path", "frame_results", "mcp_analysis",
    "comment_collection", "video_info",
)

_SUMMARY_TITLE_RE = re.compile(r"^# 每周必看 第(\d+)期 — (.*?) 分析报告")


def _load_json(path: str, default):
    if not os.path.isfile(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def rebuild_video(video_dir: str, title: str) -> dict | None:
    """重建单个视频的 stats.json 和报告，没有任何数据时返回 None"""
    stats_path = os.path.join(video_dir, "stats.json")
    old_stats = _load_json(stats_path, None)
    raw_comments = _load_json(os.path.join(video_dir, "comments.json"), [])
    raw_danmaku = _load_json(os.path.join(video_dir, "danmaku.json"), [])
    if old_stats is None and not raw_comments and not raw_danmaku:
        return None
    old_stats = old_stats or {}

    # 旧版本翻页时接口未按游标返回，comments.json 可能包含大量重复评论，按 rpid 去重
    seen_rpids = set()
    unique_raw = []
    for c in raw_comments:
        if isinstance(c, dict) and c.get("rpid") not in seen_rpids:
            seen_rpids.add(c.get("rpid"))
            unique_raw.append(c)
    if len(unique_raw) < len(raw_comments):
        print(f"    去除重复评论 {len(raw_comments) - len(unique_raw)} 条"
              f"（{len(raw_comments)} → {len(unique_raw)}）")
    comments = [comment_from_dict(c) for c in unique_raw]
    danmaku = [danmaku_from_dict(d) for d in raw_danmaku if isinstance(d, dict)]
    video_specs = old_stats.get("video_specs") or {}
    owner_mid = old_stats.get("owner_mid") or video_specs.get("owner_mid", 0)

    stats = build_statistics(comments, danmaku, video_specs=video_specs, owner_mid=owner_mid)
    for key in PRESERVED_KEYS:
        if key in old_stats:
            stats[key] = old_stats[key]

    # 旧版 comments.json 没有保存 mid / up_replied，无法重算 UP 主回复率，沿用旧值
    has_reply_info = any("mid" in c or "up_replied" in c for c in raw_comments)
    if comments and not has_reply_info and "comments" in old_stats:
        for key in ("up_reply_count", "up_reply_rate"):
            if key in old_stats["comments"]:
                stats["comments"][key] = old_stats["comments"][key]

    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    content_prompts = _load_json(os.path.join(video_dir, "content_prompts.json"), {})
    generate_video_report(stats, video_dir, title, content_prompts)
    return stats


def _series_info(run_dir: str, state: dict) -> dict | None:
    """期号信息：优先断点续传清单，旧目录没有清单时从原汇总报告标题解析"""
    if state.get("series_number"):
        return {"number": state["series_number"], "name": state.get("series_name", "")}
    report_path = os.path.join(run_dir, "report.md")
    if os.path.isfile(report_path):
        with open(report_path, "r", encoding="utf-8") as f:
            match = _SUMMARY_TITLE_RE.match(f.readline())
        if match:
            return {"number": int(match.group(1)), "name": match.group(2).strip()}
    return None


_DATA_FILES = ("stats.json", "comments.json", "danmaku.json")


def _video_dirs(run_dir: str) -> list[tuple[int, str, str]]:
    """运行目录下的视频目录 [(aid, 目录名, 目录名中的标题)]

    目录名形如 {aid}_{标题} 且包含数据文件才算，
    避免把 20260618_052343 这样的运行目录误认成视频目录。
    """
    result = []
    for entry in sorted(os.listdir(run_dir)):
        aid_str, sep, name = entry.partition("_")
        path = os.path.join(run_dir, entry)
        if (sep and aid_str.isdigit() and os.path.isdir(path)
                and any(os.path.isfile(os.path.join(path, f)) for f in _DATA_FILES)):
            result.append((int(aid_str), entry, name))
    return result


def is_run_dir(path: str) -> bool:
    """是否为一次采集的运行目录（包含视频子目录）"""
    return os.path.isdir(path) and bool(_video_dirs(path))


def rebuild_run(run_dir: str) -> int:
    """重建一个运行目录下所有视频及汇总报告，返回成功重建的视频数"""
    state = _load_json(os.path.join(run_dir, "resume_state.json"), {})
    videos_meta = state.get("videos", {})
    series_info = _series_info(run_dir, state)  # 必须在汇总报告被重写之前读取

    all_stats = []
    video_dirs = _video_dirs(run_dir)
    print(f"\n📂 {os.path.basename(os.path.normpath(run_dir))}（{len(video_dirs)} 个视频目录）")
    for aid, entry, dir_title in video_dirs:
        video_dir = os.path.join(run_dir, entry)
        old_info = _load_json(os.path.join(video_dir, "stats.json"), {}).get("video_info", {})
        title = videos_meta.get(str(aid), {}).get("title") or old_info.get("title") or dir_title
        try:
            stats = rebuild_video(video_dir, title)
        except Exception as e:
            print(f"  ✗ {title[:40]}: {e}")
            continue
        if stats is None:
            print(f"  - {title[:40]}: 无数据，跳过")
            continue
        info = stats.get("video_info", {})
        all_stats.append({
            "aid": aid, "title": title,
            "views": info.get("view", 0), "likes": info.get("like", 0),
            "stats": stats,
        })
        c = stats.get("comments", {})
        d = stats.get("danmaku", {})
        print(f"  ✓ {title[:40]}  评论 {c.get('total', 0)} / 弹幕 {d.get('total', 0)}")

    if len(all_stats) >= 2:
        _, report_path = write_summary(all_stats, run_dir, series_info)
        print(f"  📄 汇总报告: {report_path}")
    return len(all_stats)


def rebuild_all(output_root: str) -> int:
    """重建输出根目录下的所有运行目录，返回重建的视频总数"""
    if is_run_dir(output_root):
        return rebuild_run(output_root)
    total = 0
    if os.path.isdir(output_root):
        for entry in sorted(os.listdir(output_root)):
            run_dir = os.path.join(output_root, entry)
            if is_run_dir(run_dir):
                total += rebuild_run(run_dir)
    return total
