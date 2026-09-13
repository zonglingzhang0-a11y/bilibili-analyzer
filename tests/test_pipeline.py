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


class _RateLimitedClient:
    def __init__(self):
        self.requests = 0

    def get(self, url, params=None):
        self.requests += 1
        response = _FakeResponse({})
        response.status_code = 412
        return response

    def close(self):
        pass


def _patch_comment_session(monkeypatch, client):
    monkeypatch.setattr(comments, "_build_session", lambda: client)
    monkeypatch.setattr(comments, "_try_sign_params", lambda params: params)
    monkeypatch.setattr(comments.time, "sleep", lambda *_: None)


def test_rate_limit_exhaustion_is_reported_as_incomplete(monkeypatch):
    _patch_comment_session(monkeypatch, _RateLimitedClient())
    assert comments.fetch_comments(1, max_pages=5) == ([], 0)
    with pytest.raises(comments.CommentsIncomplete):
        comments.fetch_comments(1, max_pages=5, raise_on_incomplete=True)


class _FinitePagesClient:
    """3 页评论，最后一页 is_end 且 next=0"""

    def __init__(self):
        self.requests = 0

    def get(self, url, params=None):
        self.requests += 1
        start = (self.requests - 1) * 20
        replies = [{"rpid": i, "oid": 1, "mid": i, "member": {"uname": "u"},
                    "content": {"message": "好"}, "ctime": 0, "like": 0, "rcount": 2}
                   for i in range(start, start + 20)]
        last = self.requests == 3
        return _FakeResponse({"code": 0, "data": {
            "replies": replies,
            "cursor": {"next": 0 if last else self.requests + 1, "is_end": last,
                       "is_begin": self.requests == 1, "all_count": 100},
        }})

    def close(self):
        pass


def test_fetch_comments_full_pages_until_the_end(monkeypatch):
    client = _FinitePagesClient()
    _patch_comment_session(monkeypatch, client)

    result, total, info = comments.fetch_comments_full(1)

    assert len(result) == 60 and client.requests == 3
    assert info == {"strategy": "full", "collected": 60, "reported_total": 100, "sub_replies": 120}


def test_comments_only_resumes_and_skips_finished_videos(tmp_path, monkeypatch):
    run = tmp_path / "20260913_000000"
    run.mkdir()
    videos = {}
    for aid in (1, 2):
        video_dir = run / f"{aid}_视频{aid}"
        video_dir.mkdir()
        (video_dir / "stats.json").write_text(json.dumps({
            "owner_mid": 7, "comments": {"total": 6}, "video_info": {"title": f"视频{aid}"},
        }), encoding="utf-8")
        videos[str(aid)] = {"aid": aid, "title": f"视频{aid}", "state": "completed",
                            "output_dir": f"{aid}_视频{aid}"}
    (run / "resume_state.json").write_text(
        json.dumps({"series_number": 390, "total_videos": 2, "videos": videos}), encoding="utf-8")
    (run / main.RECOLLECT_STATE_FILE).write_text(
        json.dumps({"1": {"strategy": "full", "count": 500}}), encoding="utf-8")

    fetched = []

    def fake_full(aid, progress_callback=None, limiter=None):
        fetched.append(aid)
        return [make_comment(i) for i in range(30)], 50, {"strategy": "full", "collected": 30,
                                                          "reported_total": 50, "sub_replies": 0}

    monkeypatch.setattr(main, "fetch_comments_full", fake_full)

    main._recollect_comments_only(str(tmp_path), full=True)

    assert fetched == [2]
    state = json.loads((run / main.RECOLLECT_STATE_FILE).read_text(encoding="utf-8"))
    assert state["2"]["strategy"] == "full" and state["2"]["count"] == 30
    stats = json.loads((run / "2_视频2" / "stats.json").read_text(encoding="utf-8"))
    assert stats["comments"]["total"] == 30
    assert stats["comment_collection"]["strategy"] == "full"


class _FlakyNetworkClient(_FinitePagesClient):
    """前两次请求网络出错，之后正常返回 3 页评论"""

    def __init__(self, failures=2):
        super().__init__()
        self.failures = failures
        self.attempts = 0

    def get(self, url, params=None):
        import httpx
        self.attempts += 1
        if self.attempts <= self.failures:
            raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred")
        return super().get(url, params)


def test_network_errors_retry_the_same_page(monkeypatch):
    client = _FlakyNetworkClient(failures=2)
    _patch_comment_session(monkeypatch, client)

    result, total, info = comments.fetch_comments_full(1)

    assert len(result) == 60 and client.attempts == 5


def test_persistent_network_errors_are_incomplete(monkeypatch):
    import httpx
    client = _FlakyNetworkClient(failures=100)
    _patch_comment_session(monkeypatch, client)

    with pytest.raises(comments.CommentsIncomplete):
        comments.fetch_comments_full(1)
    assert client.attempts == comments.MAX_NETWORK_RETRIES + 1
    with pytest.raises(httpx.ConnectError):  # 非全量模式保持原行为：抛出原始异常
        comments.fetch_comments(1, max_pages=5)


def test_comments_only_pauses_after_consecutive_failures(tmp_path, monkeypatch):
    run = tmp_path / "20260913_000000"
    run.mkdir()
    videos = {}
    for aid in range(1, 6):
        (run / f"{aid}_v").mkdir()
        (run / f"{aid}_v" / "stats.json").write_text(json.dumps({"owner_mid": 7}), encoding="utf-8")
        videos[str(aid)] = {"aid": aid, "title": f"v{aid}", "state": "completed", "output_dir": f"{aid}_v"}
    (run / "resume_state.json").write_text(json.dumps({"total_videos": 5, "videos": videos}),
                                           encoding="utf-8")
    attempts = []

    def failing_full(aid, progress_callback=None, limiter=None):
        attempts.append(aid)
        raise comments.CommentsIncomplete("网络连续出错", 0)

    monkeypatch.setattr(main, "fetch_comments_full", failing_full)

    main._recollect_comments_only(str(tmp_path), full=True)

    assert attempts == [1, 2, 3]


def test_tee_writes_to_terminal_and_log(tmp_path):
    import io
    terminal = io.StringIO()
    log_path = tmp_path / "run.log"
    with open(log_path, "a", encoding="utf-8", buffering=1) as log_file:
        tee = main._Tee(terminal, log_file)
        print("采集中 ✓", file=tee, flush=True)
    assert terminal.getvalue() == "采集中 ✓\n"
    assert log_path.read_text(encoding="utf-8") == "采集中 ✓\n"
