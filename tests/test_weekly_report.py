import json
from collections import Counter
from datetime import datetime

import weekly_report

PUBDATE = int(datetime(2026, 9, 1, 12, 0).timestamp())


def _iso(hours_after: float) -> str:
    return datetime.fromtimestamp(PUBDATE + hours_after * 3600).isoformat()


def _make_video(run, aid, title, owner, comments, mids, danmaku_words):
    video_dir = run / f"{aid}_{title}"
    video_dir.mkdir()
    stats = {"video_info": {"title": title, "bvid": f"BV{aid}", "owner_name": owner, "view": aid * 1000,
                            "like": 1, "coin": 1, "favorite": 1, "pubdate": PUBDATE},
             "video_specs": {"duration_seconds": 120, "pages": [{"cid": 1, "duration": 120}]},
             "danmaku_peaks": [{"time": "0:30", "density": 12, "sample": ["哈哈"]}]}
    (video_dir / "stats.json").write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")
    rows = [{"rpid": i, "mid": mids[i % len(mids)], "member_name": "u", "content": text,
             "ctime": _iso(i * 0.5), "like": 100 - i, "rcount": 2, "replies": []}
            for i, text in enumerate(comments)]
    (video_dir / "comments.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    danmaku = [{"progress": 30_000 + i * 100, "content": word, "mode": 1, "color": "#FFFFFF"}
               for i, word in enumerate(danmaku_words)]
    (video_dir / "danmaku.json").write_text(json.dumps(danmaku, ensure_ascii=False), encoding="utf-8")


def _make_run(tmp_path, name, series, meme):
    run = tmp_path / name
    run.mkdir()
    videos = {}
    specs = [(1, "视频甲", "UP甲", "太好看了真棒", range(100, 160)),
             (2, "视频乙", "UP乙", "这视频真烂", range(130, 190)),
             (3, "视频丙", "UP丙", "还行吧", range(500, 560))]
    for aid, title, owner, text, mids in specs:
        comments = [f"{text} {meme}"] * 60
        _make_video(run, aid, title, owner, comments, list(mids), [meme] * 30)
        videos[str(aid)] = {"aid": aid, "title": title, "state": "completed"}
    (run / "resume_state.json").write_text(
        json.dumps({"series_number": series, "series_name": f"第{series}期", "videos": videos},
                   ensure_ascii=False), encoding="utf-8")
    return run


def test_build_report_data(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_report, "MIN_COMMENTS", 10)
    monkeypatch.setattr(weekly_report, "MEME_MIN_COUNT", 20)
    previous = _make_run(tmp_path, "20260901_000000", 100, "awsl")
    current = _make_run(tmp_path, "20260908_000000", 101, "xswl")

    assert weekly_report.find_previous_run(str(current)) == str(previous)
    data = weekly_report.build_report_data(str(current), str(previous))

    assert data["summary"]["videos"] == 3 and data["summary"]["comments"] == 180
    videos = {v["title"]: v for v in data["videos"]}
    assert videos["视频甲"]["positive_rate"] > videos["视频乙"]["positive_rate"]
    assert videos["视频乙"]["negative_rate"] > 0
    assert videos["视频甲"]["hourly"][0] == 2  # 发布后第 0 小时内 2 条（间隔半小时）
    assert videos["视频甲"]["peaks"][0]["link"] == "https://www.bilibili.com/video/BV1?t=30"
    assert not any(k.startswith("_") for k in videos["视频甲"])

    tags = [item["tag"] for item in data["insights"]]
    assert tags[:2] == ["最受好评", "争议最大"] and "本周热梗" in tags
    assert data["radar"]["rising"][0]["word"] == "xswl" and data["radar"]["rising"][0]["is_new"]
    assert data["radar"]["fading"][0]["word"] == "awsl"

    pair = data["overlap"]["pairs"][0]
    assert {pair["a"], pair["b"]} == {1, 2} and pair["shared"] == 30


def test_generate_writes_self_contained_html(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_report, "MIN_COMMENTS", 10)
    run = _make_run(tmp_path, "20260908_000000", 101, "xswl")

    path = weekly_report.generate_weekly_report(str(run))

    html = open(path, encoding="utf-8").read()
    assert "{{DATA}}" not in html and "每周必看第 101 期周报" in html
    payload = html.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert json.loads(payload)["previous"] is None


def test_copy_paste_spam_counted_once(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_report, "COPY_INSIGHT_MIN", 10)
    run = tmp_path / "20260908_000000"
    run.mkdir()
    template = "别人评论是为了交流观点，我评论只有一个观点：希望成为幸运儿"
    # 同一段抽奖模板的几种变体（空白、表情、结尾截断不同）都应算作同一段
    spam = [template, template + " [doge]", template.replace("，", "\n") + "🌚", template[:-1]] * 10
    _make_video(run, 1, "视频甲", "UP甲", spam + ["这个视频真好看"] * 30 + ["正常的评论，说说我的看法"], [1, 2], ["好"])

    video = weekly_report.analyze_video(str(run / "1_视频甲"), 1, "视频甲", embed_media=False)

    assert video["copies"] == 40 and video["copy_templates"] == 1
    assert video["copy_top"]["count"] == 40
    assert video["_words"]["幸运儿"] == 1          # 40 条刷屏只算一条
    assert video["_emojis"]["doge"] <= 1
    assert video["_words"]["好看"] == 30           # 短评论的自然重复照常计数
    summary = weekly_report._issue_summary([video])
    insights = weekly_report._insights([video], summary, None, {"pairs": []})
    assert any(item["tag"] == "复制刷屏" and item["aid"] == 1 for item in insights)


def test_meme_radar_ignores_single_video_topics_and_ids():
    def video(words):
        return {"_words": Counter(words), "_danmaku_words": Counter()}
    # 「中奖」一个视频刷了上万次、另两个视频各 1 次；「中秋」在 5 个视频里各出现 20 次
    current = [video({"中奖": 20000, "中秋": 20, "bv1bpem6geo7": 40}), video({"中奖": 2, "中秋": 20, "bv1bpem6geo7": 40}),
               video({"中奖": 2, "中秋": 20, "bv1bpem6geo7": 40}), video({"中秋": 20}), video({"中秋": 20})]
    previous = [video({"普通": 500}) for _ in range(5)]
    for v in current + previous:
        v["_examples"] = []

    radar = weekly_report._meme_radar(current, previous)

    words = [x["word"] for x in radar["rising"]]
    assert words == ["中秋"]
    assert radar["rising"][0]["now"] == 100 and radar["rising"][0]["videos"] == 5


def test_video_link_for_multi_part_video():
    pages = [{"duration": 100}, {"duration": 200}]
    assert weekly_report._video_link("BVx", pages, 150) == "https://www.bilibili.com/video/BVx?p=2&t=50"
    assert weekly_report._video_link("", pages, 10) is None


def test_refresh_later_reports_after_filling_a_gap(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_report, "MIN_COMMENTS", 10)
    _make_run(tmp_path, "20260901_000000", 100, "awsl")
    later = _make_run(tmp_path, "20260915_000000", 102, "xswl")
    weekly_report.generate_weekly_report(str(later))  # 此时与第 100 期对比
    middle = _make_run(tmp_path, "20260908_000000", 101, "xswl")

    refreshed = weekly_report.refresh_later_reports(str(middle))

    assert [r["series"] for r in refreshed] == [102]
    html = open(refreshed[0]["path"], encoding="utf-8").read()
    payload = html.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert json.loads(payload)["previous"]["series"]["number"] == 101


def test_find_previous_run_falls_back_to_nearest_earlier_issue(tmp_path):
    older = _make_run(tmp_path, "20260801_000000", 98, "awsl")
    _make_run(tmp_path, "20260915_000000", 102, "xswl")
    current = _make_run(tmp_path, "20260908_000000", 101, "xswl")
    assert weekly_report.find_previous_run(str(current)) == str(older)


def test_find_previous_run_skips_incomplete_issue(tmp_path):
    complete = _make_run(tmp_path, "20260901_000000", 100, "awsl")
    partial = _make_run(tmp_path, "20260908_000000", 101, "xswl")
    state = json.loads((partial / "resume_state.json").read_text(encoding="utf-8"))
    state["total_videos"] = 48                     # 计划 48 个，只完成了 3 个：仍在采集中
    (partial / "resume_state.json").write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    current = _make_run(tmp_path, "20260915_000000", 102, "xswl")
    assert weekly_report.find_previous_run(str(current)) == str(complete)


def test_index_page_summarizes_all_issues(tmp_path, monkeypatch):
    monkeypatch.setattr(weekly_report, "MIN_COMMENTS", 10)
    for name, series, meme in (("20260901_000000", 100, "awsl"), ("20260908_000000", 101, "xswl")):
        weekly_report.generate_weekly_report(str(_make_run(tmp_path, name, series, meme)))

    index = tmp_path / weekly_report.INDEX_NAME
    html = index.read_text(encoding="utf-8")
    assert "{{" not in html
    payload = html.split('<script id="data" type="application/json">', 1)[1].split("</script>", 1)[0]
    data = json.loads(payload)
    assert [i["series"]["number"] for i in data["issues"]] == [100, 101]
    assert data["issues"][1]["compared_with"] == 100 and data["issues"][1]["memes"][0] == "xswl"
    assert data["issues"][0]["report"] == "20260901_000000/weekly_report.html"
    assert {r["owner"] for r in data["regulars"]} == {"UP甲", "UP乙", "UP丙"}
    assert all(r["issues"] == 2 for r in data["regulars"])
