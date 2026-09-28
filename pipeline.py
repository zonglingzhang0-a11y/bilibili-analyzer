"""
采集流程：单个视频（评论 + 弹幕 + 统计 + 内容分析 + 报告）和整期每周必看

    run_series(RunOptions(series=391, full_comments=True, run_name="20260928_192613"))
"""
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime

from adaptive_retry import (
    AdaptiveRateLimiter, RetryQueue,
    create_comment_limiter, create_danmaku_limiter, create_inter_video_limiter,
)
from checkpoint_manager import CheckpointManager
from comments import fetch_comments, fetch_comments_full, fetch_comments_maximized
from content_analyzer import (
    build_content_prompts, capture_frames, download_cover, fetch_hardware_params,
    generate_mcp_task_list,
)
from danmaku import fetch_video_danmaku
from ranking import VideoInfo, fetch_weekly_videos, get_latest_series_number, get_series_info
from report_writer import generate_video_report, write_summary
from runs import completed_summary_entries, find_resumable_dir, load_resume_state
from stats import build_statistics, print_report, save_results
from weekly_report import generate_weekly_report, refresh_later_reports

# 评论每种排序默认最多采集的页数（每页 20 条）
DEFAULT_COMMENT_PAGES = 100
# 连续失败这么多个视频就暂停：多半是网络或限流问题还没过去，继续只会把后面的视频都刷成失败
MAX_CONSECUTIVE_FAILURES = 3


@dataclass
class VideoJob:
    """单个视频的采集上下文

    保存采集到的原始数据和失败项，失败重试时只补采失败的部分，
    再用完整数据重新统计、保存和生成报告。
    """
    video: VideoInfo
    output_dir: str
    dir_name: str
    no_content: bool = False
    no_frames: bool = False
    maximize_comments: bool = True
    comment_max_pages: int = DEFAULT_COMMENT_PAGES
    full_comments: bool = False
    bvid: str = ""
    owner_mid: int = 0
    video_specs: dict = field(default_factory=dict)
    cover_path: str | None = None
    comments: list = field(default_factory=list)
    comment_stats_info: dict | None = None
    danmaku: list = field(default_factory=list)
    failures: dict = field(default_factory=dict)  # {"comments" | "danmaku": 错误信息}
    stats: dict = field(default_factory=dict)
    content_prompts: dict = field(default_factory=dict)
    report_path: str | None = None


def comment_strategy(full: bool, maximize: bool, max_pages: int) -> str:
    """评论采集策略的标识，用于判断重采时某个视频是否已按同样方式采过"""
    if full:
        return "full"
    return f"{'maximized' if maximize else 'time'}:{max_pages}"


def _page_progress_printer(every_pages: int = 50):
    """全量采集可能翻几百上千页，每 N 页打印一次进度"""
    start = time.time()
    pages = 0

    def callback(collected: int, total: int):
        nonlocal pages
        pages += 1
        if pages % every_pages == 0:
            print(f"    … 已翻 {pages} 页，采到 {collected} 条"
                  f"（接口总数含楼中楼 {total}），用时 {(time.time() - start) / 60:.1f} 分钟",
                  flush=True)

    return callback


def fetch_video_comments(aid: int, *, full: bool, maximize: bool, max_pages: int,
                         limiter: AdaptiveRateLimiter = None) -> tuple[list, int, dict | None]:
    """按采集策略获取一个视频的评论，返回 (评论列表, 接口总数, 采集信息)；失败时抛出异常"""
    if full:
        print("  采集评论(全量，按时间排序翻到最后一页)...", flush=True)
        comments, total, info = fetch_comments_full(
            aid, progress_callback=_page_progress_printer(), limiter=limiter)
        print(f"  ✓ {len(comments)} 条一级评论（接口总数含楼中楼 {total}，"
              f"这些评论下的楼中楼 {info['sub_replies']} 条）")
    elif maximize:
        print("  采集评论(双模式最大化)...", end=" ")
        comments, total, info = fetch_comments_maximized(
            aid, max_pages_per_mode=max_pages, limiter=limiter)
        print(f"✓ {len(comments)} 条主评论 (去重后) / {total} 总计")
        if info:
            print(f"    mode2={info['mode2_count']} "
                  f"mode3={info['mode3_count']} "
                  f"重叠{info['overlap_count']}条 "
                  f"新增{info['mode3_count'] - info['overlap_count']}条")
    else:
        print("  采集评论...", end=" ")
        comments, total = fetch_comments(aid, max_pages=max_pages, limiter=limiter)
        info = None
        print(f"✓ {len(comments)} 条主评论 / {total} 总计")
    return comments, total, info


def _collect_comments(job: VideoJob, limiter: AdaptiveRateLimiter = None):
    """采集评论写入 job，失败时抛出异常"""
    job.comments, _, job.comment_stats_info = fetch_video_comments(
        job.video.aid, full=job.full_comments, maximize=job.maximize_comments,
        max_pages=job.comment_max_pages, limiter=limiter,
    )


def _danmaku_pages(job: VideoJob) -> list[tuple[int, int, int]]:
    """需要采集弹幕的分P [(cid, 时长秒, 在整体时间轴上的起点毫秒)]

    多P视频（有硬参数时）逐P采集，并按顺序拼接成一条时间轴；否则只采主 cid。
    """
    pages = job.video_specs.get("pages") or []
    if len(pages) > 1 and all(p.get("cid") and p.get("duration") for p in pages):
        result, offset_ms = [], 0
        for p in pages:
            result.append((p["cid"], p["duration"], offset_ms))
            offset_ms += p["duration"] * 1000
        return result
    if job.video.cid and job.video.duration > 0:
        return [(job.video.cid, job.video.duration, 0)]
    return []


def _collect_danmaku(job: VideoJob, limiter: AdaptiveRateLimiter = None):
    """采集弹幕写入 job，失败时抛出异常"""
    pages = _danmaku_pages(job)
    print(f"  采集弹幕{f'({len(pages)}P)' if len(pages) > 1 else ''}...", end=" ")
    danmaku = []
    for cid, duration, offset_ms in pages:
        page_danmaku = fetch_video_danmaku(cid, duration, limiter=limiter)
        for d in page_danmaku:
            d.progress += offset_ms
        danmaku.extend(page_danmaku)
    job.danmaku = danmaku
    print(f"✓ {len(job.danmaku)} 条弹幕")


def _capture_peak_frames(job: VideoJob, peaks: list[dict]) -> list[dict]:
    """按高潮时间所在的分P截取缩略图，结果保持 peaks 的顺序"""
    frames_dir = os.path.join(job.output_dir, "frames")
    pages = _danmaku_pages(job) or [(job.video.cid, job.video.duration, 0)]
    by_page: dict[int, list[dict]] = {}
    for peak in peaks:
        minutes, seconds = peak["time"].split(":")
        peak_ms = (int(minutes) * 60 + int(seconds)) * 1000
        index = 0
        for i, (_, _, offset_ms) in enumerate(pages):
            if peak_ms >= offset_ms:
                index = i
        by_page.setdefault(index, []).append(peak)

    results = []
    for index, page_peaks in by_page.items():
        cid, duration, offset_ms = pages[index]
        results.extend(capture_frames(
            job.bvid, page_peaks, frames_dir, cid=cid, aid=job.video.aid,
            duration_seconds=duration, time_offset_ms=offset_ms,
        ))
    order = {id(p): i for i, p in enumerate(peaks)}
    return sorted(results, key=lambda r: order.get(id(r["timestamp_info"]), 0))


def _analyze_and_save(job: VideoJob, checkpoint: CheckpointManager = None):
    """统计分析 + 高潮帧 + 保存结果 + 生成报告，并按是否有失败项更新断点状态"""
    video = job.video
    stats = build_statistics(job.comments, job.danmaku,
                             video_specs=job.video_specs, owner_mid=job.owner_mid)
    stats["video_info"] = {
        "title": video.title, "bvid": job.bvid, "owner_name": video.owner_name,
        "view": video.view, "like": video.like, "coin": video.coin,
        "favorite": video.favorite, "pubdate": video.pubdate,
    }
    # 附加评论采集统计
    if job.comment_stats_info:
        stats["comment_collection"] = job.comment_stats_info

    # ── 内容分析：弹幕高潮帧截取 ──
    frame_results = []
    if not job.no_content and not job.no_frames and job.bvid and job.danmaku:
        peaks = stats.get("danmaku_peaks", [])
        if peaks:
            top_peaks = peaks[:3]  # Top 3 高潮时刻
            print(f"  截取 {len(top_peaks)} 个高潮帧...", end=" ")
            try:
                frame_results = _capture_peak_frames(job, top_peaks)
                print(f"✓ 成功 {len(frame_results)}/{len(top_peaks)}")
            except Exception as e:
                print(f"✗ {e}")

    # ── 内容分析：生成 MCP 分析提示词 ──
    job.content_prompts = {}
    if not job.no_content:
        job.content_prompts = build_content_prompts(job.cover_path, frame_results, video.title)
        # 保存提示词供后续 MCP 分析
        if job.content_prompts:
            os.makedirs(job.output_dir, exist_ok=True)
            with open(os.path.join(job.output_dir, "content_prompts.json"), "w",
                      encoding="utf-8") as f:
                json.dump(job.content_prompts, f, ensure_ascii=False, indent=2)
        stats["video_specs"] = job.video_specs
        stats["cover_path"] = job.cover_path
        stats["frame_results"] = frame_results

    print_report(stats, video.title)
    save_results(job.comments, job.danmaku, stats, job.output_dir, video.title)
    job.stats = stats

    # ── 生成 Markdown 报告 ──
    job.report_path = None
    try:
        job.report_path = generate_video_report(stats, job.output_dir, video.title,
                                                job.content_prompts)
        if job.report_path:
            print(f"  📄 报告: {job.report_path}")
    except Exception as e:
        print(f"  ⚠️ 报告生成失败: {e}")

    # 有采集失败项时记为 failed：下次运行（断点续传）会重新采集，而不是永久跳过
    if checkpoint:
        if job.failures:
            detail = "; ".join(f"{op}: {err}" for op, err in job.failures.items())
            checkpoint.mark_video_failed(video.aid, f"部分采集失败 - {detail}", video.title)
        else:
            stats_summary = {
                "comments": stats.get("comments", {}).get("total", 0),
                "danmaku": stats.get("danmaku", {}).get("total", 0),
            }
            checkpoint.mark_video_complete(video.aid, job.dir_name, stats_summary, video.title)


def video_dir_name(aid: int, title: str) -> str:
    """视频输出目录名 {aid}_{标题}：只保留安全字符并截断

    去掉末尾的空格和点：Windows 创建目录时会静默删除它们，导致之后按原名写文件时
    找不到目录（例如标题「⚡️ 嘉 豪 の 小 曲 ⚡️」去掉表情后末尾会剩下空格）。
    """
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-（）()【】")[:40]
    return f"{aid}_{safe_title.rstrip(' .')}"


def process_video(video: VideoInfo, output_base: str, no_content: bool = False,
                  no_frames: bool = False, checkpoint: CheckpointManager = None,
                  comment_limiter: AdaptiveRateLimiter = None,
                  danmaku_limiter: AdaptiveRateLimiter = None,
                  retry_queue: RetryQueue = None,
                  maximize_comments: bool = True,
                  comment_max_pages: int = DEFAULT_COMMENT_PAGES,
                  full_comments: bool = False) -> VideoJob | None:
    """处理单个视频：采集评论 + 弹幕 + 统计 + 内容分析

    Returns:
        VideoJob（可能带有 failures，失败项已加入 retry_queue）；完全无数据时返回 None
    """
    # 清理文件名
    dir_name = video_dir_name(video.aid, video.title)
    job = VideoJob(
        video=video, output_dir=os.path.join(output_base, dir_name), dir_name=dir_name,
        no_content=no_content, no_frames=no_frames,
        maximize_comments=maximize_comments, comment_max_pages=comment_max_pages,
        full_comments=full_comments,
        bvid=video.bvid, owner_mid=video.owner_mid,
    )

    print(f"\n{'─' * 50}")
    print(f"🎬 {video.title}")
    print(f"   aid={video.aid}, cid={video.cid}, "
          f"时长={video.duration // 60}分{video.duration % 60}秒")
    print(f"{'─' * 50}")

    # ── 内容分析：硬参数采集 ──
    # --no-content 时通常跳过；但 --aid 模式缺 cid/时长时仍需要它来补全，否则采不到弹幕
    pic_url = video.pic
    if not no_content or not video.cid or not video.duration:
        print("  采集硬参数...", end=" ")
        try:
            specs = fetch_hardware_params(video.aid)
            if not no_content:
                job.video_specs = specs
            print(f"✓ {specs.get('width', '?')}x{specs.get('height', '?')} "
                  f"{specs.get('duration_seconds', '?')}s")
            # 从硬参数补充缺失字段
            job.bvid = job.bvid or specs.get("bvid", "")
            job.owner_mid = job.owner_mid or specs.get("owner_mid", 0)
            pic_url = pic_url or specs.get("pic_url", "")
            # --aid 模式未提供 cid/时长时，用第一个分P补全，弹幕才能采集
            pages = specs.get("pages") or []
            if not video.cid and pages:
                video.cid = pages[0]["cid"]
                video.duration = video.duration or pages[0].get("duration", 0)
            video.duration = video.duration or specs.get("duration_seconds", 0)
        except Exception as e:
            print(f"✗ {e}")

    # ── 内容分析：封面下载 ──
    if not no_content and pic_url:
        print("  下载封面...", end=" ")
        job.cover_path = download_cover(pic_url, os.path.join(job.output_dir, "cover.jpg"))
        print("✓" if job.cover_path else "✗ 下载失败")

    # 采集评论
    try:
        _collect_comments(job, comment_limiter)
    except Exception as e:
        print(f"✗ {e}")
        job.failures["comments"] = str(e)

    # 采集弹幕
    if _danmaku_pages(job):
        try:
            _collect_danmaku(job, danmaku_limiter)
        except Exception as e:
            print(f"✗ {e}")
            job.failures["danmaku"] = str(e)
    else:
        print("  跳过弹幕 (无 cid 或时长为0)")

    if retry_queue:
        for op, err in job.failures.items():
            retry_queue.add(video.aid, video.title, op, err, {"job": job})

    if not job.comments and not job.danmaku and not job.video_specs:
        print("  ⚠️ 无数据，跳过")
        if checkpoint:
            reason = "; ".join(job.failures.values()) or "评论、弹幕、视频规格均为空"
            checkpoint.mark_video_failed(video.aid, f"无数据：{reason}", video.title)
        return None

    _analyze_and_save(job, checkpoint)
    return job


def summary_entry(job: VideoJob) -> dict:
    """跨视频对比 / 汇总报告使用的条目"""
    return {
        "aid": job.video.aid,
        "title": job.video.title,
        "views": job.video.view,
        "likes": job.video.like,
        "stats": job.stats,
    }


def retry_failed(retry_queue: RetryQueue, all_stats: list[dict],
                 checkpoint: CheckpointManager = None,
                 comment_limiter: AdaptiveRateLimiter = None,
                 danmaku_limiter: AdaptiveRateLimiter = None,
                 inter_video_limiter: AdaptiveRateLimiter = None):
    """补采失败项，把补到的数据写回对应视频的结果、报告和汇总列表"""
    if not retry_queue.has_pending():
        return

    print(f"\n{'─' * 50}")
    print(f"🔄 重试失败项 ({retry_queue.get_pending_count()} 项)")
    print(f"{'─' * 50}")

    updated_jobs: dict[int, VideoJob] = {}
    for item in retry_queue.get_pending():
        job = item.context.get("job")
        if job is None or item.operation not in ("comments", "danmaku"):
            retry_queue.mark_retried(item, False, "不支持的重试操作")
            continue

        # 失败多半是限流导致的，重试前先冷却
        if inter_video_limiter:
            print(f"  ⏱ 冷却 {inter_video_limiter.current_delay:.1f}s...")
            inter_video_limiter.wait()
        else:
            time.sleep(60)

        print(f"  重试 [{item.operation}] {item.title[:30]} (aid={item.aid})")
        try:
            if item.operation == "comments":
                _collect_comments(job, comment_limiter)
            else:
                _collect_danmaku(job, danmaku_limiter)
        except Exception as e:
            print(f"✗ {e}")
            job.failures[item.operation] = str(e)
            retry_queue.mark_retried(item, False, str(e))
            continue

        job.failures.pop(item.operation, None)
        retry_queue.mark_retried(item, True)
        updated_jobs[job.video.aid] = job

    for aid, job in updated_jobs.items():
        print(f"\n  ♻️ 用补采数据重新生成: {job.video.title[:40]}")
        _analyze_and_save(job, checkpoint)
        entry = summary_entry(job)
        for idx, existing in enumerate(all_stats):
            if existing["aid"] == aid:
                all_stats[idx] = entry
                break
        else:
            all_stats.append(entry)


def write_weekly_report(run_dir: str):
    """生成周报网页（自动与同一输出目录中的上一期对比），并刷新改为与本期对比的后续各期；
    失败不影响采集结果"""
    try:
        path = generate_weekly_report(run_dir)
        print(f"📊 周报网页: {path}")
        for later in refresh_later_reports(run_dir):
            print(f"📊 已更新第 {later['series']} 期周报（改为与本期对比）: {later['path']}")
    except Exception as e:
        print(f"⚠️ 周报网页生成失败: {e}")


def print_rankings(comparison: dict):
    """终端打印跨视频对比排行"""
    print("\n" + "=" * 60)
    print("🏆 跨视频横向对比")
    print("=" * 60)

    rankings = comparison.get("rankings", {})
    ranking_sections = [
        ("danmaku_density", "📊 弹幕密度排行 TOP 5:"),
        ("up_interaction", "💬 UP主互动率排行 TOP 5:"),
        ("positive_sentiment", "😊 正面评论率排行 TOP 5:"),
    ]
    for key, heading in ranking_sections:
        if key in rankings:
            print(f"\n{heading}")
            for item in rankings[key][:5]:
                print(f"  #{item['rank']} {item['title'][:35]} - {item['value']}{item['unit']}")

    overall = comparison.get("overall_scores", [])
    if overall:
        print("\n🌟 综合评分 TOP 10:")
        for item in overall[:10]:
            print(f"  #{item['rank']} {item['title'][:35]} - {item['overall_score']}/100")


@dataclass
class RunOptions:
    """一次整期采集的参数（由命令行参数转换而来）"""
    output: str = "./bilibili_output"
    series: int | None = None           # 期号，None 表示最新一期
    number: int = 0                     # 最多处理的视频数，0 表示全部
    run_name: str | None = None         # 指定运行目录名；None 时自动续跑或新建
    no_content: bool = False
    no_frames: bool = False
    use_checkpoint: bool = True
    use_adaptive: bool = True
    maximize_comments: bool = True
    comment_max_pages: int = DEFAULT_COMMENT_PAGES
    full_comments: bool = False


@dataclass
class _Limiters:
    """整期采集用的三个自适应限流器（关闭自适应时都为 None）"""
    inter_video: AdaptiveRateLimiter | None = None
    comment: AdaptiveRateLimiter | None = None
    danmaku: AdaptiveRateLimiter | None = None

    @classmethod
    def create(cls, enabled: bool) -> "_Limiters":
        if not enabled:
            return cls()
        limiters = cls(create_inter_video_limiter(), create_comment_limiter(), create_danmaku_limiter())
        print(f"🔄 自适应延迟已启用 "
              f"(视频间={limiters.inter_video.current_delay}s, "
              f"评论={limiters.comment.current_delay}s/页, "
              f"弹幕={limiters.danmaku.current_delay}s/段)")
        return limiters


def _resolve_series(opts: RunOptions) -> tuple[int, str, list] | None:
    """确定期号并获取视频列表，返回 (期号, 期名, 视频列表)；失败时返回 None"""
    print("获取每周必看...")
    # 前置冷却，避免 B站限流
    print("  (等待 10 秒冷却...)")
    time.sleep(10)

    # 未指定时必须先查出最新期号（接口不带期号时返回的是第 1 期）
    series_number = opts.series
    if not series_number:
        try:
            series_number = get_latest_series_number()
        except Exception as e:
            print(f"  ✗ 获取最新期号失败: {e}\n  可以用 -s 手动指定期号")
            return None
        print(f"  最新一期: 第 {series_number} 期")

    series_name = ""
    info = get_series_info(series_number)
    if info:
        series_name = info["name"]
        print(f"  第 {info['number']} 期: {series_name} ({info['video_count']} 个视频)")

    videos = fetch_weekly_videos(series_number)
    print(f"  获取到 {len(videos)} 个视频\n")
    if opts.number > 0:
        videos = videos[:opts.number]
    return series_number, series_name, videos


def _prepare_run_dir(opts: RunOptions, series_number: int) -> str | None:
    """确定输出目录：指定了 run_name 就用它；否则断点续传时复用同一期未完成的目录，再否则新建。
    指定的目录里是别的期的数据时返回 None"""
    if opts.run_name:
        output_base = os.path.join(opts.output, opts.run_name)
        existing = load_resume_state(output_base)
        if existing and existing.get("series_number") not in (None, series_number):
            print(f"✗ 目录 {opts.run_name} 里是第 {existing['series_number']} 期的数据，"
                  f"与本次第 {series_number} 期不一致，已中止")
            return None
        print(f"📁 运行目录: {opts.run_name}" + ("（接着已有进度继续）" if existing else ""))
    else:
        output_base = find_resumable_dir(opts.output, series_number) if opts.use_checkpoint else None
        if output_base:
            print(f"📋 断点续传模式：复用目录 {os.path.basename(output_base)}")
        else:
            output_base = os.path.join(opts.output, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(output_base, exist_ok=True)
    return output_base


def _collect_videos(opts: RunOptions, videos: list, output_base: str,
                    checkpoint: CheckpointManager | None, limiters: _Limiters,
                    retry_queue: RetryQueue) -> tuple[list[dict], bool]:
    """逐个采集视频，返回 (汇总条目, 是否因连续失败而暂停)"""
    all_stats = []
    consecutive_failures = 0
    for i, v in enumerate(videos, 1):
        progress = ""
        if checkpoint:
            p = checkpoint.get_progress()
            progress = f" (总进度 {p['completed']}/{p['total']})"
        print(f"[{i}/{len(videos)}]{progress}", end="")
        try:
            job = process_video(
                v, output_base, opts.no_content, opts.no_frames,
                checkpoint=checkpoint,
                comment_limiter=limiters.comment,
                danmaku_limiter=limiters.danmaku,
                retry_queue=retry_queue,
                maximize_comments=opts.maximize_comments,
                comment_max_pages=opts.comment_max_pages,
                full_comments=opts.full_comments,
            )
        except Exception as e:
            # 单个视频的意外错误不应中断整期采集：记为失败，下次运行会重新处理
            job = None
            print(f"\n  ✗ 处理视频时出现意外错误，已跳过: {e!r}")
            if checkpoint:
                checkpoint.mark_video_failed(v.aid, f"意外错误: {e!r}", v.title)
        if job:
            all_stats.append(summary_entry(job))

        # 通知限流器：有采集失败项同样视为失败，拉长后续冷却
        succeeded = bool(job and not job.failures)
        if limiters.inter_video:
            if succeeded:
                limiters.inter_video.record_success()
            else:
                limiters.inter_video.record_failure()

        consecutive_failures = 0 if succeeded else consecutive_failures + 1
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            print(f"\n⛔ 连续 {consecutive_failures} 个视频采集失败，可能是网络异常或被限流，已暂停。"
                  "\n   请确认网络正常后重新运行同一命令，已完成的视频会自动跳过。", flush=True)
            return all_stats, True

        # 自适应视频间冷却
        if i < len(videos):
            if limiters.inter_video:
                print(f"  ⏱ 冷却 {limiters.inter_video.current_delay:.1f}s (自适应)...")
                limiters.inter_video.wait()
            else:
                time.sleep(60)
    return all_stats, False


def _write_run_reports(opts: RunOptions, all_stats: list[dict], output_base: str,
                       series_number: int, series_name: str):
    """画面分析清单、跨视频对比、汇总报告和周报网页"""
    if not opts.no_content and all_stats:
        try:
            mcp_manifest_path = generate_mcp_task_list(output_base)
            if mcp_manifest_path:
                print(f"\n📋 MCP 分析清单: {mcp_manifest_path}")
                print("  运行采集完成后可使用 mcp_integrator.py 执行画面分析")
        except Exception as e:
            print(f"\n⚠️ MCP 清单生成失败: {e}")

    if len(all_stats) >= 2:
        series_info = {"number": series_number, "name": series_name} if series_number else None
        try:
            comparison, summary_path = write_summary(all_stats, output_base, series_info)
        except Exception as e:
            print(f"\n⚠️ 汇总报告生成失败: {e}")
        else:
            if len(all_stats) >= 3:
                print_rankings(comparison)
            print(f"\n📄 汇总报告: {summary_path}")
        write_weekly_report(output_base)


def _print_run_summary(all_stats: list[dict], output_base: str):
    """终端输出每个视频的评论、弹幕数量"""
    print("\n" + "=" * 60)
    print("🏆 周热榜汇总")
    print("=" * 60)
    for item in all_stats:
        c = item["stats"].get("comments", {})
        d = item["stats"].get("danmaku", {})
        cc = item["stats"].get("comment_collection", {})
        extra = ""
        if cc.get("strategy") == "full":
            extra = f" | 评论采集: 全量 {cc.get('collected', 0)} 条"
        elif cc:
            extra = f" | 评论采集: mode2={cc.get('mode2_count', 0)} + mode3={cc.get('mode3_count', 0)}"
        print(f"\n📺 {item['title'][:40]}")
        print(f"   评论: {c.get('total', 0)} | 弹幕: {d.get('total', 0)} | "
              f"弹幕密度: {d.get('density_per_minute', 0)}/分钟{extra}")
    print(f"\n详细结果保存在: {output_base}/")


def run_series(opts: RunOptions):
    """采集一整期每周必看：逐个视频采集，失败重试，最后生成汇总报告和周报网页"""
    resolved = _resolve_series(opts)
    if resolved is None:
        return
    series_number, series_name, videos = resolved

    output_base = _prepare_run_dir(opts, series_number)
    if output_base is None:
        return

    checkpoint = None
    if opts.use_checkpoint:
        checkpoint = CheckpointManager(
            output_base, series_number=series_number,
            series_name=series_name, total_videos=len(videos),
        )
        pending_videos = checkpoint.filter_pending(videos)
        skipped = len(videos) - len(pending_videos)
        if skipped > 0:
            print(f"📋 断点续传: {skipped} 个视频已完成，跳过")
        checkpoint.print_progress()
        videos = pending_videos

    if not videos:
        print("✅ 所有视频已处理完成！")
        return

    limiters = _Limiters.create(opts.use_adaptive)
    retry_queue = RetryQueue()
    all_stats, paused = _collect_videos(opts, videos, output_base, checkpoint, limiters, retry_queue)

    # 因连续失败暂停时跳过重试：网络或限流问题多半还没恢复
    if not paused:
        retry_failed(retry_queue, all_stats, checkpoint,
                     limiters.comment, limiters.danmaku, limiters.inter_video)

    # 断点续传时，汇总应覆盖整个运行目录中已完成的视频，而不只是本次新处理的
    if checkpoint:
        all_stats = completed_summary_entries(output_base, checkpoint)

    _write_run_reports(opts, all_stats, output_base, series_number, series_name)

    retry_queue.print_summary()
    if retry_queue.has_pending() and checkpoint:
        print("  未成功的视频已标记为失败，下次运行会自动重新采集")
    if checkpoint:
        checkpoint.print_progress()
    _print_run_summary(all_stats, output_base)
