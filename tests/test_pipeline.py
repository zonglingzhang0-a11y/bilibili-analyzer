"""采集流程（网络请求全部用假函数替代）"""
import json
import os

import pytest

import comments
import main
from adaptive_retry import RetryQueue
from checkpoint_manager import CheckpointManager
from helpers import make_comment, make_danmaku, make_video


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(main.time, "sleep", lambda *_: None)


def test_failed_comments_are_retried_and_written_back(tmp_path, monkeypatch, capsys):
    calls = {"count": 0}

    def fake_comments(aid, max_pages_per_mode=100, limiter=None):
        calls["count"] += 1
        calls["pages"] = max_pages_per_mode
        if calls["count"] == 1:
            raise RuntimeError("412 限流")
        return [make_comment(1, "太好看了")], 1, {"mode2_count": 1, "mode3_count": 0,
                                               "overlap_count": 0}

    monkeypatch.setattr(main, "fetch_comments_maximized", fake_comments)
    monkeypatch.setattr(main, "fetch_video_danmaku",
                        lambda cid, duration, limiter=None: [make_danmaku(i * 1000) for i in range(30)])

    checkpoint = CheckpointManager(str(tmp_path), series_number=1, total_videos=1)
    retry_queue = RetryQueue()
    job = main.process_video(make_video(), str(tmp_path), no_content=True, checkpoint=checkpoint,
                             retry_queue=retry_queue, comment_max_pages=7)

    assert calls["pages"] == 7
    assert checkpoint._state["videos"]["123"]["state"] == "failed"
    assert retry_queue.get_pending_count() == 1

    all_stats = [main._summary_entry(job)]
    main._retry_failed(retry_queue, all_stats, checkpoint)

    assert checkpoint._state["videos"]["123"]["state"] == "completed"
    assert all_stats[0]["stats"]["comments"]["total"] == 1
    with open(os.path.join(job.output_dir, "stats.json"), encoding="utf-8") as f:
        assert json.load(f)["comments"]["total"] == 1


def test_multi_part_danmaku_is_joined_on_one_timeline(monkeypatch, tmp_path):
    fetched = []

    def fake_danmaku(cid, duration, limiter=None):
        fetched.append((cid, duration))
        return [make_danmaku(1000)]

    monkeypatch.setattr(main, "fetch_video_danmaku", fake_danmaku)
    job = main.VideoJob(video=make_video(cid=11, duration=300), output_dir=str(tmp_path),
                        dir_name="x")
    job.video_specs = {"pages": [{"cid": 11, "duration": 100}, {"cid": 22, "duration": 200}]}

    main._collect_danmaku(job)

    assert fetched == [(11, 100), (22, 200)]
    assert [d.progress for d in job.danmaku] == [1000, 101_000]


def test_peak_frames_use_the_part_they_belong_to(monkeypatch, tmp_path):
    captured = []

    def fake_capture(bvid, peaks, output_dir, cid=0, aid=0, duration_seconds=0, time_offset_ms=0):
        captured.append((cid, time_offset_ms, [p["time"] for p in peaks]))
        return [{"timestamp_info": p, "frame_path": f"{p['time']}.jpg"} for p in peaks]

    monkeypatch.setattr(main, "capture_frames", fake_capture)
    job = main.VideoJob(video=make_video(cid=11), output_dir=str(tmp_path), dir_name="x")
    job.video_specs = {"pages": [{"cid": 11, "duration": 100}, {"cid": 22, "duration": 200}]}
    peaks = [{"time": "2:30"}, {"time": "0:10"}, {"time": "1:40"}]  # 150s / 10s / 100s

    results = main._capture_peak_frames(job, peaks)

    assert sorted(captured) == [(11, 0, ["0:10"]), (22, 100_000, ["2:30", "1:40"])]
    assert [r["timestamp_info"]["time"] for r in results] == ["2:30", "0:10", "1:40"]


class _FakeResponse:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _RepeatingPageClient:
    """模拟接口忽略翻页游标：每次都返回同一页评论，但游标一直变化"""

    def __init__(self):
        self.requests = 0

    def get(self, url, params=None):
        self.requests += 1
        replies = [{"rpid": i, "oid": 1, "mid": i, "member": {"uname": "u"},
                    "content": {"message": "好"}, "ctime": 0, "like": 0, "rcount": 0}
                   for i in range(20)]
        return _FakeResponse({"code": 0, "data": {
            "replies": replies,
            "cursor": {"next": self.requests + 1, "is_end": False, "is_begin": False,
                       "all_count": 999},
        }})

    def close(self):
        pass


def test_fetch_comments_stops_when_pages_repeat(monkeypatch):
    client = _RepeatingPageClient()
    monkeypatch.setattr(comments, "_build_session", lambda: client)
    monkeypatch.setattr(comments, "_try_sign_params", lambda params: params)
    monkeypatch.setattr(comments.time, "sleep", lambda *_: None)

    result, total = comments.fetch_comments(1, max_pages=100)

    assert len(result) == 20
    assert client.requests == 2
