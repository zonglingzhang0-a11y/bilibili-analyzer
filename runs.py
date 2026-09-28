"""
运行目录管理：断点续传清单、可续跑目录查找、评论重采进度（纯本地文件操作，不联网）

一次运行对应 bilibili_output 下的一个目录，里面每个视频一个 {aid}_{标题} 子目录，
resume_state.json 记录每个视频的完成状态，comments_recollect.json 记录评论重采进度。
"""
import json
import os

from checkpoint_manager import CheckpointManager

# 评论重采的进度文件（放在运行目录下，支持中断后继续）
RECOLLECT_STATE_FILE = "comments_recollect.json"
# 日志中每次运行开始时写入的标记行
RUN_START_MARKER = "🕒 开始运行:"


def load_resume_state(run_dir: str) -> dict | None:
    """读取运行目录下的断点续传清单，不存在或损坏时返回 None"""
    manifest_path = os.path.join(run_dir, CheckpointManager.MANIFEST_FILE)
    if not os.path.isfile(manifest_path):
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def find_resumable_dir(output_root: str, series_number: int) -> str | None:
    """查找同一期、尚未完成的运行目录，优先已完成最多的

    只复用期号一致的目录：期号未知或不一致时新建目录，避免不同期的数据混在一起。
    """
    if not series_number or not os.path.isdir(output_root):
        return None

    best_match = None
    best_completed = -1

    for entry in sorted(os.listdir(output_root), reverse=True):
        entry_path = os.path.join(output_root, entry)
        if not os.path.isdir(entry_path):
            continue
        state = load_resume_state(entry_path)
        if not state or state.get("series_number") != series_number:
            continue

        videos = state.get("videos", {})
        completed = sum(1 for v in videos.values() if v.get("state") == "completed")
        total = state.get("total_videos", 0)

        # 所有视频都已处理完成 → 跳过
        if total > 0 and completed >= total:
            continue
        if completed > 0 and total == 0:
            continue

        if completed > best_completed:
            best_completed = completed
            best_match = entry_path

    return best_match


def latest_run_dir(output_root: str) -> str | None:
    """最新的、带断点续传清单的运行目录"""
    if not os.path.isdir(output_root):
        return None
    for entry in sorted(os.listdir(output_root), reverse=True):
        entry_path = os.path.join(output_root, entry)
        if os.path.isdir(entry_path) and load_resume_state(entry_path) is not None:
            return entry_path
    return None


def find_video_dir(run_dir: str, aid: int, dir_name: str = "") -> str | None:
    """定位视频输出目录：优先用清单记录的目录名，否则按 aid 前缀查找"""
    if dir_name and os.path.isdir(os.path.join(run_dir, dir_name)):
        return os.path.join(run_dir, dir_name)
    for entry in os.listdir(run_dir):
        if entry.startswith(f"{aid}_") and os.path.isdir(os.path.join(run_dir, entry)):
            return os.path.join(run_dir, entry)
    return None


def load_summary_entries(run_dir: str, videos: list[tuple[int, str, str]]) -> list[dict]:
    """从磁盘读取视频的 stats.json，组装汇总条目

    Args:
        videos: [(aid, 标题, 清单记录的目录名)]
    """
    entries = []
    for aid, title, dir_name in videos:
        video_dir = find_video_dir(run_dir, aid, dir_name)
        stats_path = os.path.join(video_dir, "stats.json") if video_dir else ""
        if not (stats_path and os.path.isfile(stats_path)):
            continue
        with open(stats_path, "r", encoding="utf-8") as f:
            video_stats = json.load(f)
        info = video_stats.get("video_info", {})
        entries.append({
            "aid": aid, "title": title or info.get("title", ""),
            "views": info.get("view", 0), "likes": info.get("like", 0),
            "stats": video_stats,
        })
    return entries


def completed_summary_entries(output_base: str, checkpoint: CheckpointManager) -> list[dict]:
    """运行目录中所有已完成视频的汇总条目（断点续传时包含之前运行完成的视频）"""
    videos = [(e["aid"], e.get("title", ""), e.get("output_dir", ""))
              for e in checkpoint.get_completed_entries()]
    return load_summary_entries(output_base, videos)


def load_recollect_state(run_dir: str) -> dict:
    path = os.path.join(run_dir, RECOLLECT_STATE_FILE)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def save_recollect_state(run_dir: str, state: dict):
    """原子写入重采进度"""
    path = os.path.join(run_dir, RECOLLECT_STATE_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
