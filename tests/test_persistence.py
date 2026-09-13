"""保存到 JSON 再读回（--comments-only / --rebuild 依赖这条链路）"""
import json

import rebuild
import stats
from comments import comment_from_dict
from danmaku import danmaku_from_dict
from helpers import make_comment, make_danmaku


def test_danmaku_round_trip(tmp_path):
    danmaku = [make_danmaku(i * 7000, color=0xFE0302 if i % 2 else 0) for i in range(50)]
    stats.save_results([], danmaku, stats.build_statistics([], danmaku), str(tmp_path))
    loaded = [danmaku_from_dict(d)
              for d in json.loads((tmp_path / "danmaku.json").read_text(encoding="utf-8"))]
    assert [(d.progress, d.color, d.ctime, d.mid_hash) for d in loaded] == \
           [(d.progress, d.color, d.ctime, d.mid_hash) for d in danmaku]
    assert stats.build_statistics([], loaded)["danmaku"]["total"] == 50


def test_comment_round_trip(tmp_path):
    comments = [make_comment(1, "好看", up_replied=True), make_comment(2, "难看")]
    stats.save_results(comments, [], {}, str(tmp_path))
    loaded = [comment_from_dict(c)
              for c in json.loads((tmp_path / "comments.json").read_text(encoding="utf-8"))]
    assert [(c.rpid, c.mid, c.ctime, c.up_replied) for c in loaded] == \
           [(1, 1, 1700000000, True), (2, 2, 1700000000, False)]


def test_comment_from_old_format():
    old = {"rpid": 5, "member_name": "u", "content": "好", "ctime": "", "like": 1, "rcount": 0,
           "replies": [{"member_name": "v", "content": "嗯", "like": 0}]}
    comment = comment_from_dict(old)
    assert comment.mid == 0 and comment.ctime == 0 and comment.replies[0].parent_rpid == 5


def test_rebuild_video_dedups_comments_and_keeps_old_fields(tmp_path):
    video_dir = tmp_path / "123_测试"
    video_dir.mkdir()
    old_comment = {"rpid": 1, "member_name": "u", "content": "太好看了", "ctime": "",
                   "like": 3, "rcount": 0, "replies": []}
    (video_dir / "comments.json").write_text(
        json.dumps([old_comment] * 20, ensure_ascii=False), encoding="utf-8")
    old_stats = {
        "comments": {"total": 20, "up_reply_count": 2, "up_reply_rate": "10.0%"},
        "mcp_analysis": {"cover": "封面描述"},
        "video_info": {"title": "测试", "view": 100},
    }
    (video_dir / "stats.json").write_text(json.dumps(old_stats, ensure_ascii=False),
                                          encoding="utf-8")

    new_stats = rebuild.rebuild_video(str(video_dir), "测试")

    assert new_stats["comments"]["total"] == 1
    assert new_stats["comments"]["sentiment"]["positive"] == 1
    assert new_stats["comments"]["up_reply_rate"] == "10.0%"  # 旧格式无 mid，沿用旧值
    assert new_stats["mcp_analysis"] == {"cover": "封面描述"}
    assert (video_dir / "report.md").is_file()
