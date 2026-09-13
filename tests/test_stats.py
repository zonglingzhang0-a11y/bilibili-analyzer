import pytest

import stats
from helpers import make_comment, make_danmaku


@pytest.mark.parametrize("text, expected", [
    ("太好了真棒", "positive"),       # 单字情感词 + 程度前缀/语气尾缀
    ("这视频真烂", "negative"),
    ("牛逼厉害", "positive"),         # 同时在停用词表里的情感词
    ("不好看", "negative"),           # 否定词
    ("不是很喜欢", "negative"),       # 否定词 + 程度副词
    ("没那么好看", "negative"),
    ("一点都不好笑", "negative"),
    ("不愧是经典", "positive"),       # 含"不"但不是否定
    ("特别好", "positive"),           # "特别"不能被当成否定词"别"
    ("差不多吧", "neutral"),          # 含单字情感词的中性词
    ("美国人", "neutral"),
    ("水平很高", "neutral"),
    ("永远的神", "positive"),         # 被 jieba 拆开的短语
    ("粗制滥造", "negative"),
    ("UP主辛苦了", "positive"),
])
def test_analyze_sentiment(text, expected):
    assert stats.analyze_sentiment(text) == expected


def test_time_heatmap_covers_whole_video_and_fills_gaps():
    danmaku = [make_danmaku(0), make_danmaku(25 * 60 * 1000)]  # 0 秒和第 25 分钟
    heatmap = stats.build_statistics([], danmaku)["danmaku"]["time_heatmap"]
    assert len(heatmap) == 25 * 6 + 1
    assert heatmap["0:00"] == 1 and heatmap["25:00"] == 1 and heatmap["12:30"] == 0


def test_sentiment_curve_keeps_every_window():
    danmaku = [make_danmaku(minute * 60 * 1000, "好看") for minute in range(10)]
    curve = stats.build_statistics([], danmaku)["sentiment_curve"]
    assert len(curve) == 10
    assert sum(point["total"] for point in curve) == 10


def test_up_reply_count_uses_up_action_and_preview_replies():
    from comments import CommentReply
    comments = [make_comment(i, up_replied=(i < 3)) for i in range(10)]
    comments[5].replies.append(CommentReply(rpid=99, mid=42, member_name="up", content="谢谢",
                                            ctime=0, like=0, parent_rpid=5))
    result = stats.build_statistics(comments, [], owner_mid=42)
    assert result["comments"]["up_reply_count"] == 4
    assert result["owner_mid"] == 42
