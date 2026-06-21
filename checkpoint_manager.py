"""
断点续传管理模块
支持中断恢复、已完成视频跳过、失败记录与重试
"""
import os
import json
import time
from datetime import datetime, timezone


class CheckpointManager:
    """管理采集进度，支持断点续传"""

    # 判定视频处理完成的文件标记
    COMPLETION_MARKER = "stats.json"
    # 主清单文件名
    MANIFEST_FILE = "resume_state.json"

    def __init__(self, output_base: str, series_number: int = None,
                 series_name: str = "", total_videos: int = 0):
        self.output_base = output_base
        self.manifest_path = os.path.join(output_base, self.MANIFEST_FILE)
        self._state: dict = {}
        self._dirty = False

        if os.path.exists(self.manifest_path):
            self._load()
            # 用新参数更新元信息
            if series_number and not self._state.get("series_number"):
                self._state["series_number"] = series_number
            if series_name and not self._state.get("series_name"):
                self._state["series_name"] = series_name
            if total_videos and not self._state.get("total_videos"):
                self._state["total_videos"] = total_videos
        else:
            self._state = {
                "run_id": datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
                "series_number": series_number,
                "series_name": series_name,
                "total_videos": total_videos,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "videos": {},
            }
            self._dirty = True

        # 自动扫描恢复：发现输出目录中有 stats.json 但清单未标记的
        self._scan_and_recover()

    # ── 内部方法 ──────────────────────────────────────────

    def _load(self):
        """从磁盘加载清单"""
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                self._state = json.load(f)
        except (json.JSONDecodeError, OSError):
            self._state = {"videos": {}}

    def _save(self):
        """原子写入清单"""
        self._state["last_updated"] = datetime.now(timezone.utc).isoformat()
        tmp = self.manifest_path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.manifest_path)  # 原子操作
        except OSError:
            pass

    def _ensure_video_entry(self, aid: int, title: str = "") -> dict:
        """获取或创建视频条目"""
        key = str(aid)
        if key not in self._state["videos"]:
            self._state["videos"][key] = {
                "aid": aid,
                "title": title,
                "state": "pending",
                "output_dir": "",
                "completed_at": None,
                "error": None,
                "stats_summary": None,
            }
        return self._state["videos"][key]

    def _scan_and_recover(self):
        """扫描输出目录，恢复已存在但未标记的视频"""
        if not os.path.isdir(self.output_base):
            return
        for entry in os.listdir(self.output_base):
            # 视频目录格式: {aid}_{title}
            if not entry[0].isdigit():
                continue
            parts = entry.split("_", 1)
            if not parts:
                continue
            try:
                aid = int(parts[0])
            except ValueError:
                continue

            video_dir = os.path.join(self.output_base, entry)
            stats_path = os.path.join(video_dir, self.COMPLETION_MARKER)

            if os.path.isfile(stats_path) and os.path.getsize(stats_path) > 10:
                # 验证是有效 JSON
                try:
                    with open(stats_path, "r", encoding="utf-8") as f:
                        json.load(f)
                except (json.JSONDecodeError, OSError):
                    continue

                entry_key = str(aid)
                existing = self._state["videos"].get(entry_key, {})
                if existing.get("state") != "completed":
                    title = parts[1] if len(parts) > 1 else ""
                    self._state["videos"][entry_key] = {
                        "aid": aid,
                        "title": title or existing.get("title", ""),
                        "state": "completed",
                        "output_dir": entry,
                        "completed_at": existing.get("completed_at"),
                        "error": None,
                        "stats_summary": existing.get("stats_summary"),
                    }
                    self._dirty = True
        if self._dirty:
            self._save()
            self._dirty = False

    # ── 公共接口 ──────────────────────────────────────────

    def is_video_complete(self, aid: int) -> bool:
        """检查视频是否已处理完成"""
        # 先查清单
        entry = self._state["videos"].get(str(aid), {})
        if entry.get("state") == "completed":
            return True

        # 再查磁盘（处理清单丢失的情况）
        if not os.path.isdir(self.output_base):
            return False
        for entry_name in os.listdir(self.output_base):
            if entry_name.startswith(f"{aid}_"):
                stats_path = os.path.join(self.output_base, entry_name,
                                          self.COMPLETION_MARKER)
                if os.path.isfile(stats_path) and os.path.getsize(stats_path) > 10:
                    try:
                        with open(stats_path, "r", encoding="utf-8") as f:
                            json.load(f)
                        return True
                    except (json.JSONDecodeError, OSError):
                        pass
        return False

    def is_video_failed(self, aid: int) -> bool:
        """检查视频是否已标记为失败"""
        return self._state["videos"].get(str(aid), {}).get("state") == "failed"

    def mark_video_complete(self, aid: int, output_dir: str = "",
                            stats_summary: dict = None, title: str = ""):
        """标记视频处理完成"""
        entry = self._ensure_video_entry(aid, title)
        entry["state"] = "completed"
        entry["output_dir"] = output_dir
        entry["completed_at"] = datetime.now(timezone.utc).isoformat()
        entry["error"] = None
        if stats_summary:
            entry["stats_summary"] = stats_summary
        self._dirty = True
        self._save()
        self._dirty = False

    def mark_video_failed(self, aid: int, error: str, title: str = ""):
        """标记视频处理失败"""
        entry = self._ensure_video_entry(aid, title)
        entry["state"] = "failed"
        entry["error"] = error
        entry["completed_at"] = None
        self._dirty = True
        self._save()
        self._dirty = False

    def filter_pending(self, videos: list) -> list:
        """过滤出待处理的视频列表"""
        pending = []
        for v in videos:
            aid = v.aid if hasattr(v, "aid") else v.get("aid", 0)
            if not self.is_video_complete(aid):
                pending.append(v)
        return pending

    def get_failed_videos(self) -> list[dict]:
        """获取所有失败视频"""
        return [
            entry for entry in self._state["videos"].values()
            if entry["state"] == "failed"
        ]

    def get_progress(self) -> dict:
        """获取进度概览"""
        videos = self._state.get("videos", {})
        total = len(videos) or self._state.get("total_videos", 0)
        completed = sum(1 for v in videos.values() if v["state"] == "completed")
        failed = sum(1 for v in videos.values() if v["state"] == "failed")
        pending = max(0, total - completed - failed)
        return {
            "total": total,
            "completed": completed,
            "failed": failed,
            "pending": pending,
            "percent": round(completed / total * 100, 1) if total > 0 else 0,
        }

    def get_completed_aids(self) -> set[int]:
        """获取所有已完成的 aid 集合"""
        aids = set()
        for entry in self._state["videos"].values():
            if entry["state"] == "completed" and entry.get("aid"):
                aids.add(entry["aid"])
        return aids

    def print_progress(self):
        """打印进度条"""
        p = self.get_progress()
        bar_width = 30
        filled = int(bar_width * p["completed"] / max(p["total"], 1))
        bar = "█" * filled + "░" * (bar_width - filled)
        print(f"\n📊 总体进度: [{bar}] {p['completed']}/{p['total']} "
              f"({p['percent']}%)  "
              f"✅{p['completed']} ❌{p['failed']} ⏳{p['pending']}")
