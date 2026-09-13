import json

import main
from checkpoint_manager import CheckpointManager


def test_progress_total_uses_planned_video_count(tmp_path):
    checkpoint = CheckpointManager(str(tmp_path), series_number=1, total_videos=50)
    checkpoint.mark_video_complete(1, "1_x", {}, "x")
    progress = checkpoint.get_progress()
    assert progress["total"] == 50
    assert progress["percent"] == 2.0


def test_failed_video_is_not_complete_even_with_stats_file(tmp_path):
    video_dir = tmp_path / "2_y"
    video_dir.mkdir()
    (video_dir / "stats.json").write_text(json.dumps({"comments": {"total": 1}}), encoding="utf-8")
    CheckpointManager(str(tmp_path), series_number=1, total_videos=5) \
        .mark_video_failed(2, "comments: 412", "y")

    reloaded = CheckpointManager(str(tmp_path), series_number=1, total_videos=5)
    assert not reloaded.is_video_complete(2)


def _make_run(root, name, series, completed):
    run = root / name
    run.mkdir()
    state = {"series_number": series, "total_videos": 10,
             "videos": {str(i): {"state": "completed"} for i in range(completed)}}
    (run / "resume_state.json").write_text(json.dumps(state), encoding="utf-8")


def test_resumable_dir_only_matches_same_series(tmp_path):
    _make_run(tmp_path, "20260101_000000", series=99, completed=1)
    _make_run(tmp_path, "20260102_000000", series=100, completed=0)

    assert main._find_resumable_dir(str(tmp_path), 99).endswith("20260101_000000")
    assert main._find_resumable_dir(str(tmp_path), 101) is None
    assert main._find_resumable_dir(str(tmp_path), None) is None
    assert main._latest_run_dir(str(tmp_path)).endswith("20260102_000000")


def test_video_dir_name_has_no_trailing_space_or_dot():
    assert main._video_dir_name(1, "⚡️ 嘉 豪 の 小 曲 ⚡️") == "1_ 嘉 豪 の 小 曲"
    assert main._video_dir_name(2, "结尾有点...") == "2_结尾有点"


def test_summary_includes_videos_completed_in_earlier_runs(tmp_path):
    checkpoint = CheckpointManager(str(tmp_path), series_number=1, total_videos=3)
    for aid, view in ((1, 100), (2, 200)):
        video_dir = tmp_path / f"{aid}_视频{aid}"
        video_dir.mkdir()
        (video_dir / "stats.json").write_text(
            json.dumps({"video_info": {"view": view}}), encoding="utf-8")
        checkpoint.mark_video_complete(aid, f"{aid}_视频{aid}", {}, f"视频{aid}")
    checkpoint.mark_video_failed(3, "412", "视频3")

    entries = main._completed_summary_entries(str(tmp_path), checkpoint)

    assert sorted((e["aid"], e["title"], e["views"]) for e in entries) == \
        [(1, "视频1", 100), (2, "视频2", 200)]
