"""
周报网页：把一期每周必看的采集结果做成可交互的单文件网页（离线可打开）

用法:
    python weekly_report.py bilibili_output/20260913_155601
    python weekly_report.py bilibili_output/20260913_155601 --compare bilibili_output/20260911_011803

不指定 --compare 时，会在同一输出目录下自动寻找上一期的运行目录做对比。
输出 <运行目录>/weekly_report.html。全部使用统计规则计算，不调用大模型。
"""
import argparse
import base64
import io
import json
import math
import os
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime

from rebuild import _load_json, _series_info, _video_dirs
from report_writer import _resolve_asset
from stats import analyze_sentiment, segment_text

TEMPLATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "report_templates", "weekly.html")
OUTPUT_NAME = "weekly_report.html"

MIN_COMMENTS = 300          # 参与好评率、争议、寿命等排行的最少一级评论数（样本太少的比例不可靠）
TIMELINE_HOURS = 168        # 评论走势显示发布后 7 天
KEYWORDS_PER_VIDEO = 6
EXAMPLE_POOL = 3000         # 每个视频保留点赞最高的若干条评论，用于给关键词找例句
MEME_MIN_COUNT = 40         # 梗雷达：本期至少出现的次数
MEME_MIN_VIDEOS = 3         # 梗雷达：至少在几个视频里出现（区分跨视频流行的梗和单个视频的话题词）
OVERLAP_MIN_SHARED = 30     # 观众重合：至少共同观众人数
LATE_NIGHT_HOURS = range(0, 6)


# ── 小工具 ────────────────────────────────────────────

def _timestamp(value) -> float | None:
    """comments.json 的 ctime 是本地时间的 ISO 字符串"""
    if isinstance(value, (int, float)):
        return float(value) or None
    try:
        return datetime.fromisoformat(value).timestamp() if value else None
    except ValueError:
        return None


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _seconds(label: str) -> int:
    minutes, seconds = label.split(":")
    return int(minutes) * 60 + int(seconds)


def _thumbnail(path: str | None, width: int = 360, quality: int = 72) -> str | None:
    """图片缩小后转成 data URI 内嵌进网页"""
    if not path or not os.path.exists(path):
        return None
    try:
        from PIL import Image
        with Image.open(path) as image:
            image = image.convert("RGB")
            if image.width > width:
                image = image.resize((width, round(image.height * width / image.width)),
                                     Image.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, "JPEG", quality=quality, optimize=True)
    except Exception:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


def _video_link(bvid: str, pages: list[dict], second: int | None = None) -> str | None:
    """B站视频链接；给了秒数时跳到对应时刻（多P视频先换算到所在分P）"""
    if not bvid:
        return None
    url = f"https://www.bilibili.com/video/{bvid}"
    if second is None:
        return url
    if len(pages) > 1:
        offset = 0
        for index, page in enumerate(pages, 1):
            duration = page.get("duration") or 0
            if second < offset + duration or index == len(pages):
                return f"{url}?p={index}&t={max(0, second - offset)}"
            offset += duration
    return f"{url}?t={second}"


def _best_comment(comments: list[dict], labels: list[str], wanted: str | None = None,
                  exclude: set[int] = frozenset()):
    """点赞最高的一条（可限定情感），过滤过短的内容和已经展示过的评论"""
    for index in sorted(range(len(comments)), key=lambda i: -comments[i].get("like", 0)):
        if index in exclude or (wanted and labels[index] != wanted):
            continue
        content = comments[index].get("content", "").strip()
        if len(content) >= 6:
            return {"text": _clip(content, 140), "like": comments[index].get("like", 0),
                    "replies": comments[index].get("rcount", 0)}
    return None


# ── 单个视频 ──────────────────────────────────────────

def analyze_video(video_dir: str, aid: int, title_hint: str, embed_media: bool) -> dict:
    """计算单个视频的展示数据，同时返回供全期汇总的中间结果（以 _ 开头的键）"""
    stats = _load_json(os.path.join(video_dir, "stats.json"), {})
    comments = _load_json(os.path.join(video_dir, "comments.json"), [])
    danmaku = _load_json(os.path.join(video_dir, "danmaku.json"), [])
    info = stats.get("video_info") or {}
    specs = stats.get("video_specs") or {}
    pages = specs.get("pages") or []
    bvid = info.get("bvid") or specs.get("bvid", "")
    pubdate = info.get("pubdate") or 0

    labels = [analyze_sentiment(c.get("content", "")) for c in comments]
    sentiment = Counter(labels)
    count = len(comments)
    sub_replies = sum(c.get("rcount", 0) for c in comments)
    positive_rate = sentiment["positive"] / count if count else 0
    negative_rate = sentiment["negative"] / count if count else 0
    reply_intensity = sub_replies / count if count else 0

    # 评论时间分布：发布后第几小时、一天中的几点
    hourly = [0] * TIMELINE_HOURS
    hour_of_day = [0] * 24
    delays = []
    for comment in comments:
        ts = _timestamp(comment.get("ctime"))
        if not ts:
            continue
        hour_of_day[datetime.fromtimestamp(ts).hour] += 1
        if pubdate:
            delay = max(0.0, (ts - pubdate) / 3600)
            delays.append(delay)
            if delay < TIMELINE_HOURS:
                hourly[int(delay)] += 1
    timed = sum(hour_of_day)
    late_night = sum(hour_of_day[h] for h in LATE_NIGHT_HOURS) / timed if timed else 0

    # 弹幕热度：10 秒一桶，每桶保留出现最多的两条弹幕作为悬停样本
    buckets = defaultdict(Counter)
    for item in danmaku:
        text = (item.get("content") or "").strip()
        if text:
            buckets[item.get("progress", 0) // 10000][text] += 1
    bucket_count = max(buckets) + 1 if buckets else 0
    heat = [sum(buckets[i].values()) for i in range(bucket_count)]
    heat_samples = [[_clip(t, 18) for t, _ in buckets[i].most_common(2)] for i in range(bucket_count)]
    duration = specs.get("duration_seconds") or 0
    minutes = (bucket_count * 10 / 60) or (duration / 60)
    danmaku_density = len(danmaku) / minutes if minutes else 0

    # 弹幕高潮（名场面）与截图
    frames = {fr.get("timestamp_info", {}).get("time"): fr.get("frame_path")
              for fr in stats.get("frame_results") or []}
    peaks = []
    for peak in (stats.get("danmaku_peaks") or [])[:5]:
        second = _seconds(peak["time"])
        image_path = _resolve_asset(frames.get(peak["time"]), video_dir, "frames")
        peaks.append({
            "time": peak["time"], "second": second, "density": peak.get("density", 0),
            "samples": [_clip(s, 24) for s in peak.get("sample", [])[:4]],
            "image": _thumbnail(image_path, 360) if embed_media else None,
            "link": _video_link(bvid, pages, second),
        })

    words = Counter()
    for comment in comments:
        words.update(segment_text(comment.get("content", "")))
    danmaku_words = Counter()
    for item in danmaku:
        danmaku_words.update(segment_text(item.get("content", "")))
    ranked = sorted(range(count), key=lambda i: -comments[i].get("like", 0))

    return {
        "aid": aid,
        "title": info.get("title") or title_hint,
        "owner": info.get("owner_name", ""),
        "bvid": bvid,
        "link": _video_link(bvid, pages),
        "pubdate": datetime.fromtimestamp(pubdate).strftime("%m-%d %H:%M") if pubdate else "",
        "view": info.get("view", 0), "like": info.get("like", 0),
        "coin": info.get("coin", 0), "favorite": info.get("favorite", 0),
        "duration": duration,
        "cover": _thumbnail(_resolve_asset(stats.get("cover_path"), video_dir), 360)
                 if embed_media else None,
        "comments": count,
        "sub_replies": sub_replies,
        "sentiment": {k: sentiment[k] for k in ("positive", "neutral", "negative")},
        "positive_rate": positive_rate,
        "negative_rate": negative_rate,
        "reply_intensity": reply_intensity,
        # 争议指数：负面占比 × (1 + 平均每条一级评论引出的楼中楼数)，差评多且吵得凶时高
        "controversy": negative_rate * 100 * (1 + reply_intensity),
        "half_life": statistics.median(delays) if delays else None,
        "late_night": late_night,
        "danmaku": len(danmaku),
        "danmaku_density": danmaku_density,
        "heat": heat,
        "heat_samples": heat_samples,
        "peaks": peaks,
        "hourly": hourly,
        "top_comments": [
            {"text": _clip(comments[i].get("content", ""), 140), "like": comments[i].get("like", 0),
             "replies": comments[i].get("rcount", 0)}
            for i in ranked[:3]
        ],
        "best_positive": _best_comment(comments, labels, "positive", set(ranked[:3])),
        "best_negative": _best_comment(comments, labels, "negative", set(ranked[:3])),
        "keywords": [],
        # 以下供全期汇总使用，不写入网页
        "_words": words,
        "_danmaku_words": danmaku_words,
        "_mids": {c["mid"] for c in comments if c.get("mid")},
        "_hour_of_day": hour_of_day,
        "_examples": [(comments[i].get("like", 0), comments[i].get("content", ""))
                      for i in ranked[:EXAMPLE_POOL]],
    }


# ── 全期汇总 ──────────────────────────────────────────

def _example_for(word: str, videos: list[dict], used: set[str]) -> str | None:
    """全期点赞最高、包含该词的评论；尽量不和前面的词重复使用同一条"""
    candidates = []
    for video in videos:
        found = 0
        for like, content in video["_examples"]:
            if word in content:
                candidates.append((like, content))
                found += 1
                if found >= 3:
                    break
    candidates.sort(reverse=True)
    for _, content in candidates:
        if content not in used:
            used.add(content)
            return _clip(content, 80)
    return _clip(candidates[0][1], 80) if candidates else None


def _assign_keywords(videos: list[dict]):
    """每个视频最有区分度的词（TF-IDF），附一条包含该词的高赞评论"""
    doc_freq = Counter()
    for video in videos:
        doc_freq.update(w for w, c in video["_words"].items() if c >= 3)
    total_videos = len(videos)
    for video in videos:
        total_words = sum(video["_words"].values()) or 1
        scores = {
            word: (count / total_words) * math.log(total_videos / doc_freq[word])
            for word, count in video["_words"].items()
            if count >= 5 and 0 < doc_freq[word] < total_videos and not word.isdigit()
        }
        for word, _ in sorted(scores.items(), key=lambda x: -x[1])[:KEYWORDS_PER_VIDEO]:
            example = next((_clip(content, 80) for _, content in video["_examples"]
                            if word in content), None)
            video["keywords"].append({"word": word, "count": video["_words"][word],
                                      "example": example})


def _issue_words(videos: list[dict]) -> tuple[Counter, Counter]:
    """全期词频（评论+弹幕）以及每个词出现在几个视频里"""
    totals, spread = Counter(), Counter()
    for video in videos:
        merged = video["_words"] + video["_danmaku_words"]
        totals.update(merged)
        spread.update(w for w, c in merged.items() if c >= 2)
    return totals, spread


def _meme_radar(current: list[dict], previous: list[dict] | None) -> dict | None:
    """梗雷达：跨多个视频流行、而且比上一期明显变多的词；以及上期流行、本期降温的词"""
    if not previous:
        return None
    now, now_spread = _issue_words(current)
    before, before_spread = _issue_words(previous)
    now_total, before_total = sum(now.values()) or 1, sum(before.values()) or 1

    def rate(counter, total, word):
        return counter.get(word, 0) / total * 100_000

    rising = []
    for word, count in now.items():
        if count < MEME_MIN_COUNT or now_spread[word] < MEME_MIN_VIDEOS or word.isdigit():
            continue
        lift = (rate(now, now_total, word) + 1) / (rate(before, before_total, word) + 1)
        if lift >= 3:
            rising.append((lift * math.log(count), word, count, before.get(word, 0),
                           now_spread[word], lift))
    rising.sort(reverse=True)

    fading = []
    for word, count in before.items():
        if count < MEME_MIN_COUNT or before_spread[word] < MEME_MIN_VIDEOS or word.isdigit():
            continue
        drop = (rate(before, before_total, word) + 1) / (rate(now, now_total, word) + 1)
        if drop >= 3:
            fading.append((drop * math.log(count), word, count, now.get(word, 0), drop))
    fading.sort(reverse=True)

    used_examples: set[str] = set()
    return {
        "rising": [{"word": w, "now": c, "before": b, "videos": s, "lift": round(l, 1),
                    "is_new": b == 0, "example": _example_for(w, current, used_examples)}
                   for _, w, c, b, s, l in rising[:15]],
        "fading": [{"word": w, "before": c, "now": n, "drop": round(d, 1)}
                   for _, w, c, n, d in fading[:10]],
    }


def _audience_overlap(videos: list[dict]) -> dict:
    """观众重合：两个视频下都发过一级评论的人数（只做整体统计，不涉及具体用户）"""
    pairs = []
    for i in range(len(videos)):
        for j in range(i + 1, len(videos)):
            a, b = videos[i]["_mids"], videos[j]["_mids"]
            if not a or not b:
                continue
            shared = len(a & b)
            if shared >= OVERLAP_MIN_SHARED:
                pairs.append({"a": videos[i]["aid"], "b": videos[j]["aid"], "shared": shared,
                              "ratio": shared / min(len(a), len(b))})
    pairs.sort(key=lambda p: -p["ratio"])
    per_user = Counter(mid for video in videos for mid in video["_mids"])
    return {
        "pairs": pairs[:12],
        "users": len(per_user),
        "multi_video_users": sum(1 for c in per_user.values() if c >= 3),
        "max_videos_per_user": max(per_user.values()) if per_user else 0,
    }


def _hour_share(videos: list[dict]) -> list[float]:
    hours = [sum(v["_hour_of_day"][h] for v in videos) for h in range(24)]
    total = sum(hours) or 1
    return [h / total for h in hours]


def _issue_summary(videos: list[dict]) -> dict:
    comments = sum(v["comments"] for v in videos)
    sentiment = Counter()
    for video in videos:
        sentiment.update(video["sentiment"])
    ranked = [v for v in videos if v["comments"] >= MIN_COMMENTS]
    half_lives = [v["half_life"] for v in ranked if v["half_life"] is not None]
    hour_share = _hour_share(videos)
    return {
        "videos": len(videos),
        "comments": comments,
        "sub_replies": sum(v["sub_replies"] for v in videos),
        "danmaku": sum(v["danmaku"] for v in videos),
        "views": sum(v["view"] for v in videos),
        "median_comments": statistics.median([v["comments"] for v in videos]) if videos else 0,
        "median_density": statistics.median([v["danmaku_density"] for v in videos]) if videos else 0,
        "positive_rate": sentiment["positive"] / comments if comments else 0,
        "negative_rate": sentiment["negative"] / comments if comments else 0,
        "late_night": sum(hour_share[h] for h in LATE_NIGHT_HOURS),
        "median_half_life": statistics.median(half_lives) if half_lives else None,
        "hour_share": hour_share,
    }


def _insights(videos: list[dict], summary: dict, radar: dict | None, overlap: dict) -> list[dict]:
    """本期看点：结论先行，每条附证据"""
    ranked = [v for v in videos if v["comments"] >= MIN_COMMENTS]
    by_aid = {v["aid"]: v for v in videos}
    items = []
    if ranked:
        best = max(ranked, key=lambda v: v["positive_rate"])
        items.append({
            "tag": "最受好评", "aid": best["aid"], "value": f"{best['positive_rate']:.1%}",
            "text": f"{best['comments']:,} 条一级评论里，正面评论占 {best['positive_rate']:.1%}，"
                    f"本期整体是 {summary['positive_rate']:.1%}。",
            "quote": best["best_positive"],
        })
        hot = max(ranked, key=lambda v: v["controversy"])
        items.append({
            "tag": "争议最大", "aid": hot["aid"], "value": f"{hot['controversy']:.0f}",
            "text": f"负面评论占 {hot['negative_rate']:.1%}（本期整体 {summary['negative_rate']:.1%}），"
                    f"平均每条一级评论引出 {hot['reply_intensity']:.1f} 条楼中楼回复。",
            "quote": hot["best_negative"],
        })
    peak_video, peak = None, None
    for video in videos:
        for candidate in video["peaks"]:
            if peak is None or candidate["density"] > peak["density"]:
                peak_video, peak = video, candidate
    if peak:
        items.append({
            "tag": "弹幕名场面", "aid": peak_video["aid"], "value": f"{peak['density']} 条/6秒",
            "text": f"第 {peak['time']} 这 6 秒里刷出 {peak['density']} 条弹幕，是本期弹幕最密集的一刻。",
            "image": peak["image"], "samples": peak["samples"], "link": peak["link"],
        })
    lived = [v for v in ranked if v["half_life"] is not None]
    if len(lived) >= 2:
        slow = max(lived, key=lambda v: v["half_life"])
        fast = min(lived, key=lambda v: v["half_life"])
        items.append({
            "tag": "热度最持久", "aid": slow["aid"], "value": f"{slow['half_life']:.0f} 小时",
            "text": f"一半的评论是在发布 {slow['half_life']:.0f} 小时之后才发出的；"
                    f"而「{_clip(fast['title'], 18)}」只用了 {fast['half_life']:.1f} 小时。",
        })
        owl = max(ranked, key=lambda v: v["late_night"])
        items.append({
            "tag": "深夜党最多", "aid": owl["aid"], "value": f"{owl['late_night']:.1%}",
            "text": f"{owl['late_night']:.1%} 的评论发在凌晨 0～6 点，本期整体是 {summary['late_night']:.1%}。",
        })
    if radar and radar["rising"]:
        top = radar["rising"][0]
        before = "上期还没有出现" if top["is_new"] else f"上期只有 {top['before']:,} 次"
        items.append({
            "tag": "本周热梗", "word": top["word"], "value": f"{top['now']:,} 次",
            "text": f"「{top['word']}」在 {top['videos']} 个视频的评论和弹幕里出现了 {top['now']:,} 次，{before}。",
            "quote": {"text": top["example"]} if top["example"] else None,
        })
    if overlap["pairs"]:
        pair = overlap["pairs"][0]
        a, b = by_aid[pair["a"]], by_aid[pair["b"]]
        items.append({
            "tag": "观众重合最高", "aid": a["aid"], "value": f"{pair['ratio']:.0%}",
            "text": f"「{_clip(a['title'], 20)}」和「{_clip(b['title'], 20)}」有 {pair['shared']:,} 位共同评论者，"
                    f"占较小一方评论人数的 {pair['ratio']:.0%}。",
        })
    return items


# ── 运行目录 ──────────────────────────────────────────

def _complete_series(series: dict) -> dict:
    """采集时获取期号信息失败会导致期名为空，这里尝试从期号列表补上（失败则保持原样）"""
    if not series.get("number") or series.get("name"):
        return series
    try:
        from ranking import fetch_weekly_series
        names = {item["number"]: item.get("name", "") for item in fetch_weekly_series()}
        return {**series, "name": names.get(series["number"], "")}
    except Exception:
        return series


def analyze_run(run_dir: str, embed_media: bool = True) -> dict:
    state = _load_json(os.path.join(run_dir, "resume_state.json"), {})
    meta = state.get("videos", {})
    videos = []
    for aid, entry, dir_title in _video_dirs(run_dir):
        title = meta.get(str(aid), {}).get("title") or dir_title
        videos.append(analyze_video(os.path.join(run_dir, entry), aid, title, embed_media))
    _assign_keywords(videos)
    return {
        "run_dir": os.path.basename(os.path.normpath(run_dir)),
        "series": _complete_series(_series_info(run_dir, state) or {}),
        "videos": videos,
        "summary": _issue_summary(videos),
    }


def find_previous_run(run_dir: str) -> str | None:
    """在同一输出目录下寻找上一期（期号减一）的运行目录，优先完成视频最多的"""
    series = (_load_json(os.path.join(run_dir, "resume_state.json"), {}) or {}).get("series_number")
    if not series:
        return None
    root = os.path.dirname(os.path.normpath(run_dir))
    best, best_done = None, -1
    for entry in os.listdir(root):
        candidate = os.path.join(root, entry)
        state = _load_json(os.path.join(candidate, "resume_state.json"), None)
        if not state or state.get("series_number") != series - 1:
            continue
        done = sum(1 for v in state.get("videos", {}).values() if v.get("state") == "completed")
        if done > best_done:
            best, best_done = candidate, done
    return best


def _public(video: dict) -> dict:
    return {k: v for k, v in video.items() if not k.startswith("_")}


def build_report_data(run_dir: str, compare_dir: str | None = None) -> dict:
    current = analyze_run(run_dir)
    previous = analyze_run(compare_dir, embed_media=False) if compare_dir else None
    radar = _meme_radar(current["videos"], previous["videos"] if previous else None)
    overlap = _audience_overlap(current["videos"])

    regulars = []
    if previous:
        before = defaultdict(list)
        for video in previous["videos"]:
            if video["owner"]:
                before[video["owner"]].append(video["title"])
        for video in current["videos"]:
            if video["owner"] in before:
                regulars.append({"owner": video["owner"], "now": video["title"],
                                 "before": before[video["owner"]], "aid": video["aid"]})

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "series": current["series"],
        "run_dir": current["run_dir"],
        "summary": current["summary"],
        "previous": {"series": previous["series"], "summary": previous["summary"]} if previous else None,
        "insights": _insights(current["videos"], current["summary"], radar, overlap),
        "videos": [_public(v) for v in current["videos"]],
        "radar": radar,
        "overlap": overlap,
        "regulars": regulars,
        "min_comments": MIN_COMMENTS,
    }


def generate_weekly_report(run_dir: str, compare_dir: str | None = None,
                           output_path: str | None = None) -> str:
    """生成周报网页，返回文件路径。compare_dir 为 None 时自动寻找上一期"""
    if compare_dir is None:
        compare_dir = find_previous_run(run_dir)
    data = build_report_data(run_dir, compare_dir)
    with open(TEMPLATE_PATH, encoding="utf-8") as f:
        template = f.read()
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    series = data["series"]
    title = f"每周必看第 {series['number']} 期周报" if series.get("number") else "每周必看周报"
    html = template.replace("{{TITLE}}", title).replace("{{DATA}}", payload)
    output_path = output_path or os.path.join(run_dir, OUTPUT_NAME)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    return output_path


def main():
    if sys.stdout.encoding != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="生成每周必看周报网页")
    parser.add_argument("run_dir", help="运行目录，如 bilibili_output/20260913_155601")
    parser.add_argument("--compare", default=None,
                        help="用于对比的上一期运行目录（默认自动寻找）")
    parser.add_argument("-o", "--output", default=None, help="输出文件（默认 <运行目录>/weekly_report.html）")
    args = parser.parse_args()
    compare = args.compare or find_previous_run(args.run_dir)
    print(f"生成周报: {args.run_dir}" + (f"（对比 {os.path.basename(compare)}）" if compare else "（未找到上一期，不做对比）"))
    path = generate_weekly_report(args.run_dir, compare, args.output)
    print(f"📄 周报网页: {path}（{os.path.getsize(path) / 1e6:.1f} MB）")


if __name__ == "__main__":
    main()
