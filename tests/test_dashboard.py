import json
from datetime import datetime, timedelta, timezone

import dashboard
from main import RECOLLECT_STATE_FILE, RUN_START_MARKER

PIPELINE_LOG = f"""{RUN_START_MARKER} 2026-01-01 00:00:00  参数: -s 389 --full-comments
[1/3] (总进度 0/3)
──────────────────────────────────────────────────
🎬 视频A
  采集硬参数... ✓ 1920x1080 300s
  采集评论(全量，按时间排序翻到最后一页)...
  ✓ 900 条一级评论（接口总数含楼中楼 2000，这些评论下的楼中楼 800 条）
  采集弹幕... ✓ 500 条弹幕
  📄 报告: ./x/report.md
  ⏱ 冷却 30.0s (自适应)...
[2/3] (总进度 1/3)
──────────────────────────────────────────────────
🎬 视频B
  采集硬参数... ✓ 1920x1080 300s
  采集评论(全量，按时间排序翻到最后一页)...
  网络出错（ReadError），15s 后重试第 1 次（已采 400 条）...
    … 已翻 50 页，采到 1000 条（接口总数含楼中楼 5000），用时 1.8 分钟
"""


def _write_run(tmp_path, videos, recollect=None, log=""):
    run = tmp_path / "20260911_180000"
    run.mkdir()
    (run / "resume_state.json").write_text(
        json.dumps({"series_number": 389, "total_videos": 3, "videos": videos}), encoding="utf-8")
    if recollect is not None:
        (run / RECOLLECT_STATE_FILE).write_text(json.dumps(recollect), encoding="utf-8")
    (run / "run.log").write_text(log, encoding="utf-8")
    return run


VIDEO_LIST = [{"aid": 1, "title": "视频A", "total": 2000},
              {"aid": 2, "title": "视频B", "total": 5000},
              {"aid": 3, "title": "视频C", "total": 3000}]


def test_parse_pipeline_log_tracks_current_video_stage_and_events():
    log = dashboard.parse_log(PIPELINE_LOG.splitlines())

    assert log["mode"] == "pipeline"
    assert log["current_title"] == "视频B" and log["stage"] == "评论"
    assert log["page"] == {"pages": 50, "collected": 1000, "total": 5000, "minutes": 1.8}
    assert any("网络出错" in e for e in log["events"])
    assert not log["finished"]
    assert log["started_at"] == datetime(2026, 1, 1).timestamp()


def test_parse_log_only_uses_latest_run():
    lines = ("🎬 旧视频\n  ✗ 失败\n本次完成 1 个\n" + PIPELINE_LOG).splitlines()
    log = dashboard.parse_log(lines)
    assert not log["finished"] and not log["failed_titles"]


def test_build_status_for_pipeline_run(tmp_path):
    completed_at = datetime.now(timezone.utc).isoformat()
    videos = {"1": {"title": "视频A", "state": "completed", "completed_at": completed_at,
                    "stats_summary": {"comments": 900, "danmaku": 500}}}
    run = _write_run(tmp_path, videos, log=PIPELINE_LOG)

    status = dashboard.build_status(str(run), str(run / "run.log"), VIDEO_LIST)

    assert [r["status"] for r in status["rows"]] == ["done", "running", "pending"]
    assert status["rows"][0]["danmaku"] == 500
    assert status["done"] == 1 and status["done_comments"] == 900
    assert status["volume_percent"] == 20.0
    assert status["current"]["title"] == "视频B" and status["current"]["pages"] == 50


def test_build_status_for_recollect_run(tmp_path):
    log = (f"{RUN_START_MARKER} 2026-01-01 00:00:00  参数: --comments-only --full-comments\n"
           "📋 评论重采模式: 20260911_180000（策略 full）\n"
           "[1/3] 视频A - 已按full采集过（900 条），跳过\n"
           "[2/3] 视频B\n"
           "  ✗ 网络连续出错，保留原有数据\n"
           "[3/3] 视频C\n")
    recollect = {"1": {"strategy": "full", "count": 900,
                       "finished_at": (datetime.now() - timedelta(hours=1)).isoformat()}}
    videos = {str(v["aid"]): {"title": v["title"], "state": "completed"} for v in VIDEO_LIST}
    run = _write_run(tmp_path, videos, recollect=recollect, log=log)

    status = dashboard.build_status(str(run), str(run / "run.log"), VIDEO_LIST)

    assert status["mode"] == "recollect"
    assert [r["status"] for r in status["rows"]] == ["done", "failed", "running"]
    assert status["rows"][0]["comments"] == 900
