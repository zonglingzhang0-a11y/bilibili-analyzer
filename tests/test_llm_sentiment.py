import json

import pytest

import llm_sentiment
import weekly_report


def _make_video(run, aid, texts):
    video_dir = run / f"{aid}_视频{aid}"
    video_dir.mkdir(parents=True)
    rows = [{"rpid": aid * 1000 + i, "mid": i, "content": t, "like": 100 - i, "rcount": 0}
            for i, t in enumerate(texts)]
    (video_dir / "comments.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return video_dir


class FakeAPI:
    """按文字规则给标签的假接口：含「好」为正面夸赞，含「典」为负面批评，其余中性讨论"""

    def __init__(self, drop_first=False, error_after=None):
        self.calls = []
        self.drop_first = drop_first
        self.error_after = error_after

    def __call__(self, client, key, model, texts):
        self.calls.append(list(texts))
        if self.error_after is not None and len(self.calls) > self.error_after:
            raise llm_sentiment.LLMError("账户余额不足（402）")
        labels = {}
        for i, text in enumerate(texts):
            if self.drop_first and len(self.calls) == 1 and i == 0:
                continue  # 模拟模型漏掉一条
            labels[str(i)] = "正1" if "好" in text else "负2" if "典" in text else "中4"
        usage = {"prompt_tokens": 100, "prompt_cache_hit_tokens": 60, "completion_tokens": 20}
        return json.dumps({"labels": labels}, ensure_ascii=False), usage


def test_parse_labels_filters_malformed_entries():
    content = json.dumps({"labels": {"0": "正1", "1": "好3", "2": "中9", "3": "负2", "9": "正1", "x": "中4"}})
    assert llm_sentiment.parse_labels(content, 4) == {0: "正1", 3: "负2"}
    assert llm_sentiment.parse_labels("不是 JSON", 4) == {}
    assert llm_sentiment.decode("负2") == ("negative", "批评不满")


def test_sample_is_stable_and_extends():
    comments = [{"rpid": i, "content": f"评论{i}"} for i in range(50)]
    small = {c["rpid"] for c in llm_sentiment.select_comments(comments, 10)}
    large = {c["rpid"] for c in llm_sentiment.select_comments(comments, 20)}
    assert len(small) == 10 and small < large
    assert len(llm_sentiment.select_comments(comments, None)) == 50


def test_annotate_dedupes_resumes_and_retries_missing(tmp_path, monkeypatch):
    run = tmp_path / "20260908_000000"
    texts = ["真好看", "典中典", "这是什么"] + ["复制的抽奖模板一模一样"] * 30
    video_dir = _make_video(run, 1, texts)
    api = FakeAPI(drop_first=True)
    monkeypatch.setattr(llm_sentiment, "_post_chat", api)

    usage = llm_sentiment.annotate_run(str(run), "k", sample=None, workers=2)

    sent = [t for call in api.calls for t in call]
    assert sent.count("复制的抽奖模板一模一样") == 1       # 相同文字只发一次
    assert len(api.calls) == 2                            # 漏掉的一条单独补一次
    labels = llm_sentiment.load_labels(str(video_dir))
    assert len(labels) == 33 and labels["1000"] == "正1" and labels["1001"] == "负2"
    assert usage.requests == 2 and usage.cost > 0

    # 再跑一次：全部标过，不再请求
    api.calls.clear()
    llm_sentiment.annotate_run(str(run), "k", sample=None)
    assert api.calls == []


def test_annotate_stops_on_fatal_error_and_keeps_progress(tmp_path, monkeypatch):
    run = tmp_path / "20260908_000000"
    _make_video(run, 1, [f"第{i}条评论好" for i in range(10)])
    _make_video(run, 2, [f"另一个视频的第{i}条评论" for i in range(10)])
    monkeypatch.setattr(llm_sentiment, "BATCH_SIZE", 10)
    monkeypatch.setattr(llm_sentiment, "_post_chat", FakeAPI(error_after=1))

    llm_sentiment.annotate_run(str(run), "k", sample=None, workers=1)

    done = [len(llm_sentiment.load_labels(str(d))) for d in sorted(run.iterdir())]
    assert sorted(done) == [0, 10]


def test_load_key_prefers_env(tmp_path, monkeypatch):
    path = tmp_path / ".deepseek_key"
    llm_sentiment.save_key("  from-file \n", str(path))
    monkeypatch.delenv(llm_sentiment.KEY_ENV, raising=False)
    assert llm_sentiment.load_key(str(path)) == "from-file"
    monkeypatch.setenv(llm_sentiment.KEY_ENV, "from-env")
    assert llm_sentiment.load_key(str(path)) == "from-env"
    monkeypatch.delenv(llm_sentiment.KEY_ENV)
    assert llm_sentiment.load_key(str(tmp_path / "missing")) is None


def test_weekly_report_compares_llm_with_dictionary(tmp_path, monkeypatch):
    run = tmp_path / "20260908_000000"
    texts = ["太好看了真棒"] * 5 + ["蚌埠住了家人们"] * 5 + ["这个视频的剪辑节奏"] * 10
    video_dir = _make_video(run, 1, texts)
    (video_dir / "stats.json").write_text("{}", encoding="utf-8")
    labels = {str(1000 + i): ("正1" if i < 10 else "中4") for i in range(20)}
    llm_sentiment.save_labels(str(video_dir), labels, "deepseek-chat")
    (run / "resume_state.json").write_text(json.dumps({"series_number": 101, "videos": {}}), encoding="utf-8")

    data = weekly_report.build_report_data(str(run))

    llm = data["llm"]
    assert llm["sampled"] == 20 and llm["full"]
    assert llm["positive_rate"] == pytest.approx(0.5)
    # 「蚌埠住了」词典判不出来，大模型判为正面
    assert llm["confusion"]["neutral>positive"] >= 5
    assert any(x["dict"] == "neutral" and x["llm"] == "positive" for x in llm["examples"])
    assert data["summary"]["llm_positive_rate"] == pytest.approx(0.5)
    video = data["videos"][0]
    assert video["llm"]["n"] == 20 and not any(k.startswith("_") for k in video["llm"])
