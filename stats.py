"""
统计分析模块
词云、情感分析、热度曲线、弹幕类型分布等
"""
import json
import os
from collections import Counter
from datetime import datetime
from functools import lru_cache

import jieba

from comments import Comment
from danmaku import Danmaku


# 停用词列表（segment_text 已过滤单字，这里只需要多字词）
STOP_WORDS = {
    "因为", "所以", "虽然", "然而", "只是", "然后", "接着", "之后", "之前",
    "已经", "正在", "将要", "必须", "应该", "可能", "大概", "也许",
    "什么", "怎么", "怎么样", "为什么", "如果", "否则", "至于",
    "哪里", "对于", "根据", "除了", "按照", "比方", "比如", "经过",
    "我们", "你们", "他们", "她们", "它们", "大家",
}
# 常见无意义词
STOP_WORDS.update([
    "www", "com", "cn", "http", "https", "jpg", "png", "gif",
    "up", "up主", "视频", "真的", "感觉", "觉得", "就是", "不是",
    "这个", "那个", "一个", "什么", "自己", "怎么", "可以", "没有",
    "知道", "好像", "还是", "这么", "那么", "如果", "应该", "可能",
    "还有", "已经", "但是", "因为", "所以", "然后", "最后",
    "第一", "第二", "第三", "一下", "过来", "过去", "出来", "起来",
    "哈哈哈", "哈哈哈哈", "卧槽", "牛逼", "厉害", "有点", "有点意思",
])

# 情感词典
POSITIVE_WORDS = {
    # 基础好评
    "好", "棒", "厉害", "好看", "喜欢", "支持", "感动", "泪目", "爱了",
    "牛", "赞", "绝了", "神作", "精彩", "优秀", "舒服", "开心", "快乐",
    "帅", "美", "可爱", "暖心", "治愈", "震撼", "惊艳", "燃", "爽",
    "经典", "稳", "点赞", "好评", "吹爆", "安利", "推荐", "值得",
    "真香", "不错", "还行", "挺好", "可以", "确实",
    # 扩展
    "三连", "硬币", "投币", "已投", "下次一定", "关注", "追番",
    "有才", "牛逼", "太强", "无敌", "完美", "细节", "用心",
    "良心", "辛苦了", "respect", "赞美", "名作", "满分",
    "太棒", "好活", "当赏", "优雅", "享受", "专业", "到位",
    "硬核", "干货", "真实", "泪奔", "破防", "心动", "封神",
    "还原", "致敬", "yyds", "永远的神", "超神", "到位", "上档次",
    "行云流水", "一气呵成", "登峰造极", "炉火纯青", "出神入化",
    "入木三分", "鞭辟入里", "四两拨千斤", "化腐朽为神奇",
    "赞不绝口", "拍案叫绝", "叹为观止", "喜闻乐见", "大快人心",
    "正道的光", "国士无双", "天下第一", "一骑绝尘", "无人能及",
    "激动", "热血", "沸腾", "燃爆", "炸裂", "起飞",
    "帅气", "好听", "好玩", "有趣", "有意思", "美丽", "感谢", "谢谢", "学到了",
}
NEGATIVE_WORDS = {
    # 基础差评
    "差", "烂", "恶心", "垃圾", "无聊", "难看", "讨厌", "吐了",
    "拉胯", "下头", "尴尬", "傻", "蠢", "烦", "气", "坑", "假",
    "水", "受不了", "无语", "失望", "浪费时间",
    "差评", "不行", "不好", "差劲", "什么鬼", "离谱", "迷惑",
    # 扩展
    "抄袭", "敷衍", "粗糙", "质量差", "翻车", "退步",
    "注水", "画质差", "配音烂", "辣眼睛", "毁三观", "不适",
    "反感", "排斥", "抵制", "举报", "踩", "拉黑",
    "扁平化", "同质化", "审美疲劳", "低级", "低俗", "恶俗",
    "炒作", "标题党", "营销号", "引流", "恰饭", "广告",
    "误导", "片面", "断章取义", "以偏概全", "混淆视听",
    "不知所云", "莫名其妙", "牵强附会", "生搬硬套",
    "粗制滥造", "滥竽充数", "东施效颦", "狗尾续貂",
    "阴间", "阴乐", "配音像摔炮", "绷不住",
    "难听", "没意思",
}

# 否定词：出现在情感词前面时翻转极性
NEGATION_WORDS = {
    "不", "没", "没有", "别", "不太", "不是", "并不", "毫不", "不够", "不怎么",
    "没那么", "从不", "绝不", "并非", "不算", "一点也不", "一点都不",
}
# 回溯否定词时可以跳过的程度副词/虚词，如「不是很好」「没那么好看」
_NEGATION_SKIP = {
    "很", "太", "那么", "这么", "怎么", "特别", "非常", "真的", "真", "是",
    "也", "都", "有点", "有些", "够", "算", "咋",
}
# 情感词前后常见的修饰，用于从「太好了」「真棒」「好烦啊」中取出核心词
_DEGREE_PREFIXES = ("真的", "非常", "特别", "超级", "实在", "真", "太", "很", "超",
                    "挺", "最", "好", "巨", "贼", "蛮")
_TAIL_SUFFIXES = ("死了", "了", "啊", "呀", "吧", "哦", "的", "啦", "哇")
# 三字及以上的情感短语，jieba 往往会拆开，先整体匹配
_SENTIMENT_PHRASES = sorted(
    [(w, 1) for w in POSITIVE_WORDS if len(w) >= 3] +
    [(w, -1) for w in NEGATIVE_WORDS if len(w) >= 3],
    key=lambda x: -len(x[0]),
)


@lru_cache(maxsize=200_000)
def _cut(text: str) -> tuple[str, ...]:
    """jieba 分词（带缓存：同一批弹幕会在词频、情感、主题差异中被反复分词，且重复文本很多）"""
    return tuple(jieba.lcut(text))


def segment_text(text: str) -> list[str]:
    """中文分词 + 去停用词"""
    words = (w.strip() for w in _cut(text))
    return [w for w in words if len(w) >= 2 and w not in STOP_WORDS]


def _word_polarity(word: str) -> int:
    """单个词的情感极性：1 正面 / -1 负面 / 0 无"""
    if word in POSITIVE_WORDS:
        return 1
    if word in NEGATIVE_WORDS:
        return -1
    return 0


def _token_polarity(token: str) -> tuple[int, bool]:
    """分词结果的情感极性，返回 (极性, 词内是否自带否定)

    依次尝试：原词 → 去掉语气尾缀 → 去掉程度前缀 → 两者都去掉，
    再处理「不喜欢」这类否定词与情感词粘在一起的情况。
    """
    candidates = [token]
    for suf in _TAIL_SUFFIXES:
        if len(token) > len(suf) and token.endswith(suf):
            candidates.append(token[:-len(suf)])
            break
    for base in list(candidates):
        for pre in _DEGREE_PREFIXES:
            if len(base) > len(pre) and base.startswith(pre):
                candidates.append(base[len(pre):])
                break
    for c in candidates:
        polarity = _word_polarity(c)
        if polarity:
            return polarity, False
    for neg in ("不", "没"):
        if len(token) > 1 and token.startswith(neg):
            polarity = _word_polarity(token[1:])
            if polarity:
                return polarity, True
    return 0, False


@lru_cache(maxsize=200_000)
def analyze_sentiment(text: str) -> str:
    """词典法情感分析，返回 'positive', 'negative', 'neutral'

    - 三字以上短语整体匹配（如「永远的神」「粗制滥造」）
    - 其余按 jieba 分词匹配，保留单字情感词（好/棒/烂/坑…）
    - 情感词前 3 个词内出现否定词（可跳过程度副词）时翻转极性，如「不好看」「不是很喜欢」
    """
    score_pos = score_neg = 0

    for phrase, polarity in _SENTIMENT_PHRASES:
        if phrase in text:
            hits = text.count(phrase)
            if polarity > 0:
                score_pos += hits
            else:
                score_neg += hits
            text = text.replace(phrase, "，")

    tokens = [t.strip() for t in _cut(text)]
    for i, tok in enumerate(tokens):
        if not tok:
            continue
        polarity, negated = _token_polarity(tok)
        if not polarity:
            continue
        if not negated:
            for prev in reversed(tokens[max(0, i - 3):i]):
                if prev in NEGATION_WORDS:
                    negated = True
                    break
                if prev not in _NEGATION_SKIP:
                    break
        if negated:
            polarity = -polarity
        if polarity > 0:
            score_pos += 1
        else:
            score_neg += 1

    if score_pos > score_neg:
        return "positive"
    elif score_neg > score_pos:
        return "negative"
    return "neutral"


def detect_danmaku_peaks(danmaku: list[Danmaku], window_sec: int = 6,
                         threshold_sigma: float = 2.0) -> list[dict]:
    """检测弹幕高潮时刻

    Args:
        danmaku: 弹幕列表
        window_sec: 滑动窗口大小（秒）
        threshold_sigma: 峰值阈值 = 均值 + threshold_sigma * 标准差

    Returns:
        高潮时刻列表 [{"time", "density", "sample"}, ...]
    """
    if not danmaku:
        return []

    # 计算每个窗口的弹幕密度
    window_ms = window_sec * 1000
    max_time = max(d.progress for d in danmaku)
    bucket_count = max(1, max_time // window_ms + 1)
    buckets = [0] * bucket_count

    for d in danmaku:
        idx = min(d.progress // window_ms, bucket_count - 1)
        buckets[idx] += 1

    # 计算均值和标准差
    mean = sum(buckets) / len(buckets)
    variance = sum((b - mean) ** 2 for b in buckets) / len(buckets)
    std = variance ** 0.5 if variance > 0 else 1
    threshold = mean + threshold_sigma * std

    # 找出峰值
    peaks = []
    for i, count in enumerate(buckets):
        if count >= threshold and count >= 3:  # 至少 3 条才算是峰值
            start_ms = i * window_ms
            # 收集该窗口内的弹幕样本
            samples = [
                d.content for d in danmaku
                if start_ms <= d.progress < start_ms + window_ms
            ][:8]  # 最多 8 条样本
            peaks.append({
                "time": f"{start_ms // 60000}:{start_ms % 60000 // 1000:02d}",
                "density": count,
                "sample": samples,
            })

    # 合并相邻峰值（合并间隔 < 2 个窗口的峰值）
    merged = []
    for p in peaks:
        if merged:
            last = merged[-1]
            last_start = (int(last["time"].split(":")[0]) * 60 +
                          int(last["time"].split(":")[1]))
            curr_start = (int(p["time"].split(":")[0]) * 60 +
                          int(p["time"].split(":")[1]))
            if curr_start - last_start <= window_sec * 2:
                # 合并：保留密度更高的，扩展 sample
                if p["density"] > last["density"]:
                    last["time"] = p["time"]
                    last["density"] = p["density"]
                last["sample"] = (last["sample"] + p["sample"])[:8]
                continue
        merged.append(p)

    return sorted(merged, key=lambda x: -x["density"])


def build_sentiment_curve(danmaku: list[Danmaku], window_sec: int = 30) -> list[dict]:
    """构建弹幕情感随视频时间轴的变化曲线

    Args:
        danmaku: 弹幕列表
        window_sec: 窗口大小（秒）

    Returns:
        [{"time", "positive", "neutral", "negative"}, ...]
    """
    if not danmaku:
        return []

    window_ms = window_sec * 1000
    max_time = max(d.progress for d in danmaku)
    bucket_count = max(1, max_time // window_ms + 1)
    buckets = [{"positive": 0, "neutral": 0, "negative": 0} for _ in range(bucket_count)]

    for d in danmaku:
        idx = min(d.progress // window_ms, bucket_count - 1)
        s = analyze_sentiment(d.content)
        buckets[idx][s] += 1

    curve = []
    for i, b in enumerate(buckets):
        total = b["positive"] + b["neutral"] + b["negative"]
        if total > 0:
            curve.append({
                "time": f"{i * window_sec // 60}:{i * window_sec % 60:02d}",
                "positive": b["positive"],
                "neutral": b["neutral"],
                "negative": b["negative"],
                "total": total,
            })

    return curve


def build_comment_danmaku_divergence(
    comments: list[Comment],
    danmaku: list[Danmaku],
    top_n: int = 20,
) -> dict:
    """分析评论和弹幕的主题差异

    对每个词计算「评论特有度」= 评论词频 / (评论词频 + 弹幕词频)
    值 > 0.7 表示该词主要在评论中出现（深度讨论）
    值 < 0.3 表示该词主要在弹幕中出现（即时反应）

    Returns:
        {"comment_unique": [...], "danmaku_unique": [...], "shared": [...]}
    """
    # 分词统计
    comment_freq = Counter()
    for c in comments:
        comment_freq.update(segment_text(c.content))

    danmaku_freq = Counter()
    for d in danmaku:
        danmaku_freq.update(segment_text(d.content))

    # 计算每个词的特有度
    all_words = set(comment_freq.keys()) | set(danmaku_freq.keys())
    word_scores = {}
    for w in all_words:
        cf = comment_freq.get(w, 0)
        df = danmaku_freq.get(w, 0)
        total = cf + df
        if total < 3:  # 滤除低频词
            continue
        word_scores[w] = {
            "comment_freq": cf,
            "danmaku_freq": df,
            "ratio": round(cf / total, 3) if total > 0 else 0.5,
        }

    # 排序分类
    sorted_words = sorted(word_scores.items(), key=lambda x: x[1]["ratio"])
    comment_unique = [(w, s["ratio"]) for w, s in sorted_words if s["ratio"] > 0.7]
    danmaku_unique = [(w, s["ratio"]) for w, s in sorted_words if s["ratio"] < 0.3]
    shared = [(w, s["ratio"]) for w, s in sorted_words if 0.3 <= s["ratio"] <= 0.7]

    return {
        "comment_unique_words": sorted(comment_unique, key=lambda x: -x[1])[:top_n],
        "danmaku_unique_words": sorted(danmaku_unique, key=lambda x: x[1])[:top_n],
        "shared_words": sorted(shared, key=lambda x: -min(x[1], 1 - x[1]))[:top_n],
    }


def build_cross_video_comparison(all_video_data: list[dict]) -> dict:
    """跨视频横向对比

    Args:
        all_video_data: [{"aid", "title", "stats", "views", "likes", ...}, ...]

    Returns:
        {"rankings": {...}, "overall_scores": [...]}
    """
    valid = []
    for v in all_video_data:
        s = v.get("stats", {})
        comments = s.get("comments", {})
        danmaku = s.get("danmaku", {})
        if not comments and not danmaku:
            continue
        valid.append({
            "aid": v.get("aid", 0),
            "title": v.get("title", ""),
            "views": v.get("views", 0),
            "likes": v.get("likes", 0),
            "comment_count": comments.get("total", 0),
            "comment_reply_total": comments.get("total_replies", 0),
            "up_reply_rate": float(comments.get("up_reply_rate", "0%").rstrip("%")),
            "positive_rate": (comments.get("sentiment", {}).get("positive", 0) /
                              max(sum(comments.get("sentiment", {}).values()), 1) * 100),
            "avg_comment_likes": comments.get("avg_likes", 0),
            "danmaku_count": danmaku.get("total", 0),
            "danmaku_density": danmaku.get("density_per_minute", 0),
            "scroll_danmaku_rate": (danmaku.get("mode_distribution", {}).get("滚动", 0) /
                                     max(sum(danmaku.get("mode_distribution", {}).values()), 1) * 100),
        })

    if not valid:
        return {"rankings": {}, "overall_scores": []}

    # 排名维度：key -> (数据字段, 格式化方式)
    ranking_dims = {
        "danmaku_density": ("danmaku_density", "条/分钟"),
        "up_interaction": ("up_reply_rate", "%"),
        "positive_sentiment": ("positive_rate", "%"),
        "comment_depth": ("avg_comment_likes", "赞"),
    }

    rankings = {}
    for dim_key, (data_key, unit) in ranking_dims.items():
        sorted_list = sorted(valid, key=lambda x: -x[data_key])
        rankings[dim_key] = [
            {"rank": i + 1, "aid": v["aid"], "title": v["title"],
             "value": round(v[data_key], 1), "unit": unit}
            for i, v in enumerate(sorted_list)
        ]

    # 综合评分（各维度标准化后等权加权）
    def normalize(values):
        vmax = max(values)
        vmin = min(values)
        if vmax == vmin:
            return [50.0] * len(values)
        return [(v - vmin) / (vmax - vmin) * 100 for v in values]

    score_metrics = ["danmaku_density", "up_reply_rate", "positive_rate", "avg_comment_likes"]
    scores = {v["aid"]: 0.0 for v in valid}
    for metric in score_metrics:
        vals = [v[metric] for v in valid]
        norm = normalize(vals)
        for vi, v in enumerate(valid):
            scores[v["aid"]] += norm[vi]

    overall = sorted(valid, key=lambda x: -scores[x["aid"]])
    for i, v in enumerate(overall):
        v["overall_score"] = round(scores[v["aid"]] / len(score_metrics), 1)
        v["rank"] = i + 1

    return {
        "rankings": rankings,
        "overall_scores": [{"rank": v["rank"], "aid": v["aid"], "title": v["title"],
                            "overall_score": v["overall_score"]}
                           for v in overall],
    }


def build_statistics(comments: list[Comment], danmaku: list[Danmaku],
                     video_specs: dict = None, owner_mid: int = 0) -> dict:
    """构建完整的统计数据"""
    stats = {}
    if owner_mid:
        stats["owner_mid"] = owner_mid  # 保存下来，重算统计（--comments-only）时需要

    # === 视频硬参数 ===
    if video_specs:
        stats["video_specs"] = video_specs

    # === 评论统计 ===
    if comments:
        total_comments = len(comments)
        total_replies = sum(len(c.replies) for c in comments)
        avg_likes = sum(c.like for c in comments) / total_comments if total_comments else 0
        # 优先使用接口的 up_action.reply；预览回复（每条最多约3条）里出现 UP 主也算
        up_reply_count = sum(
            1 for c in comments
            if getattr(c, "up_replied", False)
            or (owner_mid > 0 and any(r.mid == owner_mid for r in c.replies))
        )

        # 情感分布
        sentiment_counts = Counter()
        for c in comments:
            sentiment_counts[analyze_sentiment(c.content)] += 1

        # 评论高频词
        comment_words = []
        for c in comments:
            comment_words.extend(segment_text(c.content))
        comment_word_freq = Counter(comment_words).most_common(50)

        # 最长/最短评论
        sorted_by_len = sorted(comments, key=lambda x: len(x.content), reverse=True)

        stats["comments"] = {
            "total": total_comments,
            "total_replies": total_replies,
            "avg_likes": round(avg_likes, 1),
            "up_reply_count": up_reply_count,
            "up_reply_rate": f"{up_reply_count / total_comments * 100:.1f}%" if total_comments else "0%",
            "sentiment": {
                "positive": sentiment_counts.get("positive", 0),
                "neutral": sentiment_counts.get("neutral", 0),
                "negative": sentiment_counts.get("negative", 0),
            },
            "top_words": comment_word_freq[:30],
            "longest_comment": {
                "content": sorted_by_len[0].content[:200] if sorted_by_len else "",
                "length": len(sorted_by_len[0].content) if sorted_by_len else 0,
                "likes": sorted_by_len[0].like if sorted_by_len else 0,
            } if sorted_by_len else None,
        }

    # === 弹幕统计 ===
    if danmaku:
        total_danmaku = len(danmaku)
        # 弹幕类型分布
        mode_names = {1: "滚动", 4: "底部", 5: "顶部", 6: "逆向", 7: "高级"}
        mode_counts = Counter()
        for d in danmaku:
            mode_counts[mode_names.get(d.mode, f"未知({d.mode})")] += 1

        # 弹幕高频词
        dm_words = []
        for d in danmaku:
            dm_words.extend(segment_text(d.content))
        dm_word_freq = Counter(dm_words).most_common(50)

        # 时间段热度分布（按 10 秒分桶）
        time_buckets = Counter()
        for d in danmaku:
            bucket = d.progress // 10000  # 10 秒一个桶
            time_buckets[bucket] += 1

        # 弹幕密度（条/分钟）
        total_minutes = max(d.progress for d in danmaku) / 60000 if danmaku else 1
        density = total_danmaku / total_minutes if total_minutes > 0 else 0

        # 弹幕池分布
        pool_counts = Counter(d.pool for d in danmaku)

        # 弹幕颜色分布 (Top 10)
        color_counts = Counter(f"#{d.color:06X}" for d in danmaku if d.color)

        stats["danmaku"] = {
            "total": total_danmaku,
            "density_per_minute": round(density, 1),
            "mode_distribution": dict(mode_counts.most_common()),
            "pool_distribution": {
                "普通": pool_counts.get(0, 0),
                "字幕": pool_counts.get(1, 0),
                "特殊": pool_counts.get(2, 0),
            },
            "top_colors": color_counts.most_common(10),
            "top_words": dm_word_freq[:30],
            # 覆盖完整时长（空桶补 0，保证曲线横轴是均匀时间）；上限 10000 桶≈28 小时
            "time_heatmap": {f"{k * 10 // 60}:{(k * 10) % 60:02d}": time_buckets.get(k, 0)
                             for k in range(min(max(time_buckets) + 1, 10000))},
        }

    # === 弹幕高潮检测 ===
    if danmaku:
        peaks = detect_danmaku_peaks(danmaku)
        stats["danmaku_peaks"] = peaks[:5]  # Top 5 高潮时刻

        # 情感曲线（60 秒一个窗口：控制数据量，同时不丢弃任何弹幕）
        stats["sentiment_curve"] = build_sentiment_curve(danmaku, window_sec=60)

    # === 评论弹幕主题差异 ===
    if comments and danmaku:
        divergence = build_comment_danmaku_divergence(comments, danmaku)
        stats["divergence"] = divergence

    return stats


def print_report(stats: dict, video_title: str = ""):
    """打印统计报告"""
    title = f" 统计报告" + (f" - {video_title}" if video_title else "")
    print("=" * 60)
    print(title)
    print("=" * 60)

    if "video_specs" in stats:
        vs = stats["video_specs"]
        pages_info = f" (分P: {vs.get('pages_count', 0)})" if vs.get('pages_count', 1) > 1 else ""
        print(f"\n🎞️ 视频参数")
        print(f"  分辨率: {vs.get('width', '?')}x{vs.get('height', '?')}  "
              f"时长: {vs.get('duration_seconds', 0) // 60}分{vs.get('duration_seconds', 0) % 60}秒{pages_info}")
        if vs.get("desc"):
            print(f"  简介: {vs['desc'][:100]}")

    if "cover_path" in stats and stats["cover_path"]:
        print(f"\n🖼️ 封面已下载: {stats['cover_path']}")

    if "frame_results" in stats and stats["frame_results"]:
        print(f"\n📸 高潮帧截图: {len(stats['frame_results'])} 张")
        for fr in stats["frame_results"]:
            ti = fr.get("timestamp_info", {})
            print(f"  - {ti.get('time', '?')} (密度: {ti.get('density', 0)}条/6秒)")

    if "comments" in stats:
        c = stats["comments"]
        print(f"\n📝 评论统计")
        print(f"  主评论数: {c['total']}  子回复数: {c['total_replies']}")
        print(f"  平均点赞: {c['avg_likes']}  UP主回复数: {c['up_reply_count']} ({c['up_reply_rate']})")
        s = c["sentiment"]
        total_s = sum(s.values())
        if total_s > 0:
            print(f"  情感分布: 😊正面 {s['positive']}({s['positive']/total_s*100:.0f}%)  "
                  f"😐中立 {s['neutral']}({s['neutral']/total_s*100:.0f}%)  "
                  f"😞负面 {s['negative']}({s['negative']/total_s*100:.0f}%)")
        print(f"  高频词 TOP 10: {', '.join(w for w, _ in c['top_words'][:10])}")

    if "danmaku" in stats:
        d = stats["danmaku"]
        print(f"\n💬 弹幕统计")
        print(f"  弹幕总数: {d['total']}  密度: {d['density_per_minute']} 条/分钟")
        print(f"  类型分布: {d['mode_distribution']}")
        if d.get("top_colors"):
            colors_str = ", ".join(f"{c}({n})" for c, n in d["top_colors"][:5])
            print(f"  热门颜色: {colors_str}")
        if d["top_words"]:
            print(f"  高频词 TOP 10: {', '.join(w for w, _ in d['top_words'][:10])}")

    if "danmaku_peaks" in stats and stats["danmaku_peaks"]:
        peaks = stats["danmaku_peaks"]
        print(f"\n🔥 弹幕高潮时刻 TOP {len(peaks)}:")
        for i, p in enumerate(peaks, 1):
            samples = " | ".join(p["sample"][:4])
            print(f"  {i}. {p['time']} ({p['density']}条/6秒) - {samples}")

    if "divergence" in stats:
        div = stats["divergence"]
        print(f"\n🔍 评论 vs 弹幕 主题差异:")
        if div.get("comment_unique_words"):
            print(f"  评论特有词: {', '.join(w for w, _ in div['comment_unique_words'][:8])}")
        if div.get("danmaku_unique_words"):
            print(f"  弹幕特有词: {', '.join(w for w, _ in div['danmaku_unique_words'][:8])}")
        if div.get("shared_words"):
            print(f"  共同词: {', '.join(w for w, _ in div['shared_words'][:8])}")

    print("\n" + "=" * 60)


def save_results(
    comments: list[Comment],
    danmaku: list[Danmaku],
    stats: dict,
    output_dir: str,
    video_title: str = "",
):
    """保存采集结果到文件"""
    os.makedirs(output_dir, exist_ok=True)

    # 保存评论
    if comments:
        with open(os.path.join(output_dir, "comments.json"), "w", encoding="utf-8") as f:
            json.dump([{
                "rpid": c.rpid,
                "mid": c.mid,
                "member_name": c.member_name,
                "content": c.content,
                "ctime": datetime.fromtimestamp(c.ctime).isoformat() if c.ctime else "",
                "like": c.like,
                "rcount": c.rcount,
                "up_replied": getattr(c, "up_replied", False),
                "replies": [{
                    "rpid": r.rpid,
                    "mid": r.mid,
                    "member_name": r.member_name,
                    "content": r.content,
                    "ctime": datetime.fromtimestamp(r.ctime).isoformat() if r.ctime else "",
                    "like": r.like,
                } for r in c.replies],
            } for c in comments], f, ensure_ascii=False, indent=2)

    # 保存弹幕
    if danmaku:
        with open(os.path.join(output_dir, "danmaku.json"), "w", encoding="utf-8") as f:
            json.dump([{
                "id": d.id,
                "progress": d.progress,
                "time": f"{d.progress // 60000}:{(d.progress % 60000) // 1000:02d}",
                "mode": d.mode,
                "fontsize": d.fontsize,
                "color": f"#{d.color:06X}" if d.color else "",
                "mid_hash": d.mid_hash,
                "content": d.content,
                "ctime": datetime.fromtimestamp(d.ctime).isoformat() if d.ctime else "",
                "weight": d.weight,
                "pool": d.pool,
            } for d in danmaku], f, ensure_ascii=False, indent=2)

    # 保存统计
    with open(os.path.join(output_dir, "stats.json"), "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)

    print(f"\n结果已保存到: {output_dir}/")
