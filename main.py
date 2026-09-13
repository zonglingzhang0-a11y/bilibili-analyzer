"""
B站周热榜评论 & 弹幕统计分析 - 主入口
"""
import argparse
import os
import sys
import json
import time
from dataclasses import dataclass, field
from datetime import datetime

# 确保 Windows GBK 终端能输出 emoji
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

# 确保能找到模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ranking import VideoInfo, fetch_weekly_videos, fetch_weekly_series, get_series_info
from comments import fetch_comments, fetch_comments_maximized, check_login
from danmaku import fetch_video_danmaku, danmaku_from_dict
from stats import build_statistics, print_report, save_results
from content_analyzer import (
    fetch_hardware_params, download_cover, capture_frames, build_content_prompts,
    generate_mcp_task_list,
)
from report_writer import generate_video_report, write_summary
from rebuild import PRESERVED_KEYS, rebuild_all
from checkpoint_manager import CheckpointManager
from adaptive_retry import (
    AdaptiveRateLimiter, RetryQueue,
    create_comment_limiter, create_danmaku_limiter, create_inter_video_limiter,
)

# 评论每种排序默认最多采集的页数（每页 20 条）
DEFAULT_COMMENT_PAGES = 100


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


def _warn_if_not_logged_in():
    """登录凭证缺失或失效时提示：未登录会话每种排序只能拿到约 3 条评论"""
    status = check_login()
    if status is True:
        return
    if status is False:
        print("⚠️ 登录凭证 SESSDATA 已失效（当前为未登录状态）")
    else:
        print("⚠️ 未配置登录凭证 SESSDATA（或登录状态检查失败）")
    print("   未登录时每个视频每种排序只能采到约 3 条评论，评论数据会严重不全。")
    print("   请更新环境变量 BILI_SESSDATA 或项目根目录 .sessdata，"
          "可运行 python bili_auth.py 自检\n")


def _collect_comments(job: VideoJob, limiter: AdaptiveRateLimiter = None):
    """采集评论写入 job，失败时抛出异常"""
    aid = job.video.aid
    if job.maximize_comments:
        print("  采集评论(双模式最大化)...", end=" ")
        comments, comment_total, info = fetch_comments_maximized(
            aid, max_pages_per_mode=job.comment_max_pages, limiter=limiter,
        )
        print(f"✓ {len(comments)} 条主评论 (去重后) / {comment_total} 总计")
        if info:
            print(f"    mode2={info['mode2_count']} "
                  f"mode3={info['mode3_count']} "
                  f"重叠{info['overlap_count']}条 "
                  f"新增{info['mode3_count'] - info['overlap_count']}条")
    else:
        print("  采集评论...", end=" ")
        comments, comment_total = fetch_comments(
            aid, max_pages=job.comment_max_pages, limiter=limiter,
        )
        info = None
        print(f"✓ {len(comments)} 条主评论 / {comment_total} 总计")
    job.comments = comments
    job.comment_stats_info = info


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


def process_video(video: VideoInfo, output_base: str, no_content: bool = False,
                  no_frames: bool = False, checkpoint: CheckpointManager = None,
                  comment_limiter: AdaptiveRateLimiter = None,
                  danmaku_limiter: AdaptiveRateLimiter = None,
                  retry_queue: RetryQueue = None,
                  maximize_comments: bool = True,
                  comment_max_pages: int = DEFAULT_COMMENT_PAGES) -> VideoJob | None:
    """处理单个视频：采集评论 + 弹幕 + 统计 + 内容分析

    Returns:
        VideoJob（可能带有 failures，失败项已加入 retry_queue）；完全无数据时返回 None
    """
    # 清理文件名
    safe_title = "".join(c for c in video.title if c.isalnum() or c in " _-（）()【】")[:40]
    dir_name = f"{video.aid}_{safe_title}"
    job = VideoJob(
        video=video, output_dir=os.path.join(output_base, dir_name), dir_name=dir_name,
        no_content=no_content, no_frames=no_frames,
        maximize_comments=maximize_comments, comment_max_pages=comment_max_pages,
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


def _summary_entry(job: VideoJob) -> dict:
    """跨视频对比 / 汇总报告使用的条目"""
    return {
        "aid": job.video.aid,
        "title": job.video.title,
        "views": job.video.view,
        "likes": job.video.like,
        "stats": job.stats,
    }


def _retry_failed(retry_queue: RetryQueue, all_stats: list[dict],
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
        entry = _summary_entry(job)
        for idx, existing in enumerate(all_stats):
            if existing["aid"] == aid:
                all_stats[idx] = entry
                break
        else:
            all_stats.append(entry)


def _load_resume_state(run_dir: str) -> dict | None:
    """读取运行目录下的断点续传清单，不存在或损坏时返回 None"""
    manifest_path = os.path.join(run_dir, CheckpointManager.MANIFEST_FILE)
    if not os.path.isfile(manifest_path):
        return None
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _find_resumable_dir(output_root: str, series_number: int) -> str | None:
    """查找同一期、尚未完成的运行目录，优先已完成最多的

    只复用期号一致的目录：期号未知或不一致时新建目录，避免不同期的数据混在一起。
    """
    if not series_number or not os.path.isdir(output_root):
        return None

    best_match = None
    best_completed = -1

    for entry in sorted(os.listdir(output_root), reverse=True):
        entry_path = os.path.join(output_root, entry)
        if not os.path.isdir(entry_path):
            continue
        state = _load_resume_state(entry_path)
        if not state or state.get("series_number") != series_number:
            continue

        videos = state.get("videos", {})
        completed = sum(1 for v in videos.values() if v.get("state") == "completed")
        total = state.get("total_videos", 0)

        # 所有视频都已处理完成 → 跳过
        if total > 0 and completed >= total:
            continue
        if completed > 0 and total == 0:
            continue

        if completed > best_completed:
            best_completed = completed
            best_match = entry_path

    return best_match


def _latest_run_dir(output_root: str) -> str | None:
    """最新的、带断点续传清单的运行目录"""
    if not os.path.isdir(output_root):
        return None
    for entry in sorted(os.listdir(output_root), reverse=True):
        entry_path = os.path.join(output_root, entry)
        if os.path.isdir(entry_path) and _load_resume_state(entry_path) is not None:
            return entry_path
    return None


def _find_video_dir(run_dir: str, aid: int, dir_name: str = "") -> str | None:
    """定位视频输出目录：优先用清单记录的目录名，否则按 aid 前缀查找"""
    if dir_name and os.path.isdir(os.path.join(run_dir, dir_name)):
        return os.path.join(run_dir, dir_name)
    for entry in os.listdir(run_dir):
        if entry.startswith(f"{aid}_") and os.path.isdir(os.path.join(run_dir, entry)):
            return os.path.join(run_dir, entry)
    return None


def _recollect_comments_only(output_root: str, maximize: bool = True,
                             comment_max_pages: int = DEFAULT_COMMENT_PAGES):
    """仅重新采集最新一次运行中已完成视频的评论，更新 stats 和报告"""
    run_dir = _latest_run_dir(output_root)
    if not run_dir:
        print("错误: 找不到可用的运行目录（需包含 resume_state.json）")
        return

    print(f"📋 评论重采模式: {os.path.basename(run_dir)}")
    state = _load_resume_state(run_dir) or {}
    completed = [
        (int(aid), entry.get("title", ""), entry.get("output_dir", ""))
        for aid, entry in state.get("videos", {}).items()
        if entry.get("state") == "completed"
    ]
    if not completed:
        print("无已完成视频可重新采集")
        return

    print(f"  共 {len(completed)} 个视频需要重新采集评论\n")

    for i, (aid, title, dir_name) in enumerate(completed, 1):
        video_dir = _find_video_dir(run_dir, aid, dir_name)
        if not video_dir:
            print(f"[{i}/{len(completed)}] {title[:30]} - ⚠️ 目录不存在，跳过")
            continue

        print(f"[{i}/{len(completed)}] {title[:40]}")

        # 读取现有 stats
        stats_path = os.path.join(video_dir, "stats.json")
        stats = {}
        if os.path.isfile(stats_path):
            with open(stats_path, "r", encoding="utf-8") as f:
                stats = json.load(f)
        old_comment_total = stats.get("comments", {}).get("total", 0)

        # 重新采集评论；失败或一条没拿到时保留原数据，避免覆盖成空
        comment_stats_info = None
        try:
            if maximize:
                print("  采集评论(双模式)...", end=" ")
                comments, _, comment_stats_info = fetch_comments_maximized(
                    aid, max_pages_per_mode=comment_max_pages)
            else:
                print("  采集评论...", end=" ")
                comments, _ = fetch_comments(aid, max_pages=comment_max_pages)
        except Exception as e:
            print(f"✗ {e}，保留原有数据")
            continue
        if not comments and old_comment_total > 0:
            print(f"✗ 未采集到评论（原有 {old_comment_total} 条），保留原有数据")
            continue
        print(f"✓ {len(comments)} 条")

        # 读取现有弹幕
        danmaku_path = os.path.join(video_dir, "danmaku.json")
        danmaku = []
        if os.path.isfile(danmaku_path):
            with open(danmaku_path, "r", encoding="utf-8") as f:
                danmaku = [danmaku_from_dict(d) for d in json.load(f) if isinstance(d, dict)]

        # 重新统计分析（旧数据没有保存 owner_mid 时从视频信息补取）
        video_specs = stats.get("video_specs", {})
        owner_mid = stats.get("owner_mid") or video_specs.get("owner_mid", 0)
        if not owner_mid:
            try:
                owner_mid = fetch_hardware_params(aid).get("owner_mid", 0)
            except Exception:
                pass
        new_stats = build_statistics(comments, danmaku,
                                     video_specs=video_specs, owner_mid=owner_mid)
        if comment_stats_info:
            new_stats["comment_collection"] = comment_stats_info
        # 保留内容分析 / MCP 分析结果等无法从评论弹幕重算的字段
        for key in PRESERVED_KEYS:
            if key in stats and not (key == "comment_collection" and comment_stats_info):
                new_stats[key] = stats[key]

        print_report(new_stats, title)
        save_results(comments, danmaku, new_stats, video_dir, title)

        # 重新生成报告
        prompts_path = os.path.join(video_dir, "content_prompts.json")
        content_prompts = {}
        if os.path.isfile(prompts_path):
            with open(prompts_path, "r", encoding="utf-8") as f:
                content_prompts = json.load(f)

        try:
            report_path = generate_video_report(new_stats, video_dir, title, content_prompts)
            if report_path:
                print(f"  📄 报告已更新: {report_path}")
        except Exception as e:
            print(f"  ⚠️ 报告生成失败: {e}")

        # 视频间冷却
        if i < len(completed):
            time.sleep(15)

    # 重新生成汇总
    print("\n重新生成汇总报告...")
    all_stats_objs = []
    for aid, title, dir_name in completed:
        video_dir = _find_video_dir(run_dir, aid, dir_name)
        stats_path = os.path.join(video_dir, "stats.json") if video_dir else ""
        if stats_path and os.path.isfile(stats_path):
            with open(stats_path, "r", encoding="utf-8") as f:
                video_stats = json.load(f)
            info = video_stats.get("video_info", {})
            all_stats_objs.append({
                "aid": aid, "title": title,
                "views": info.get("view", 0), "likes": info.get("like", 0),
                "stats": video_stats,
            })

    if len(all_stats_objs) >= 2:
        series_num = state.get("series_number")
        si = {"number": series_num, "name": state.get("series_name", "")} if series_num else None
        try:
            _, spath = write_summary(all_stats_objs, run_dir, si)
            print(f"  📄 汇总报告已更新: {spath}")
        except Exception as e:
            print(f"  ⚠️ 汇总报告失败: {e}")

    print("\n✅ 评论重采完成!")


def _print_rankings(comparison: dict):
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


def main():
    parser = argparse.ArgumentParser(description="B站周热榜评论弹幕统计分析")
    parser.add_argument("-s", "--series", type=int, default=None,
                        help="指定每周必看期号，不指定则获取最新一期")
    parser.add_argument("-n", "--number", type=int, default=0,
                        help="最多处理的视频数量 (默认0=全部)")
    parser.add_argument("-l", "--limit", type=int, default=0,
                        help=f"评论每种排序最多采集的页数，每页20条 "
                             f"(默认0={DEFAULT_COMMENT_PAGES}页)")
    parser.add_argument("-o", "--output", type=str, default="./bilibili_output",
                        help="输出目录 (默认 ./bilibili_output)")
    parser.add_argument("--list", action="store_true",
                        help="仅列出现有期号列表")
    parser.add_argument("--aid", type=int, default=None,
                        help="直接指定视频 aid（跳过周热榜）")
    parser.add_argument("--cid", type=int, default=None,
                        help="直接指定视频 cid（配合 --aid 使用，不填则自动获取第一个分P）")
    parser.add_argument("--duration", type=int, default=0,
                        help="视频时长（秒，配合 --aid 使用，不填则自动获取）")
    parser.add_argument("--title", type=str, default="",
                        help="视频标题（配合 --aid 使用）")
    parser.add_argument("--no-content", action="store_true",
                        help="跳过内容分析（封面+硬参数+高潮帧）")
    parser.add_argument("--no-frames", action="store_true",
                        help="仅封面分析，不截取视频帧")
    parser.add_argument("--no-checkpoint", action="store_true",
                        help="禁用断点续传（每次重新采集全部）")
    parser.add_argument("--no-adaptive", action="store_true",
                        help="禁用自适应延迟（使用固定延迟）")
    parser.add_argument("--no-maximize-comments", action="store_true",
                        help="禁用双模式评论采集（仅用 mode=2）")
    parser.add_argument("--comments-only", action="store_true",
                        help="仅重新采集已处理视频的评论（跳过弹幕/内容分析）")
    parser.add_argument("--rebuild", nargs="?", const="", default=None, metavar="RUN_DIR",
                        help="离线重建：用已保存的评论/弹幕重新统计并生成报告，不联网。"
                             "指定运行目录则只重建该目录，否则重建 -o 下全部")
    args = parser.parse_args()

    comment_max_pages = args.limit if args.limit > 0 else DEFAULT_COMMENT_PAGES
    maximize_comments = not args.no_maximize_comments

    # 列出期号
    if args.list:
        print("获取每周必看期号列表...")
        series_list = fetch_weekly_series()
        for s in sorted(series_list, key=lambda x: x["number"], reverse=True)[:20]:
            print(f"  第 {s['number']} 期: {s['name']} ({s.get('subject', '')})")
        return

    # 离线重建（不联网，放在登录检查之前）
    if args.rebuild is not None:
        target = args.rebuild or args.output
        count = rebuild_all(target)
        print(f"\n✅ 离线重建完成：共 {count} 个视频")
        return

    _warn_if_not_logged_in()

    # 直接采集模式
    if args.aid:
        video = VideoInfo(
            aid=args.aid, cid=args.cid or 0, bvid="", title=args.title or "手动指定",
            owner_name="", owner_mid=0, view=0, danmaku=0, reply=0,
            favorite=0, coin=0, share=0, like=0,
            duration=args.duration, width=0, height=0, pic="", pubdate=0,
        )
        output_base = os.path.join(args.output, datetime.now().strftime("%Y%m%d_%H%M%S"))
        process_video(video, output_base, args.no_content, args.no_frames,
                      maximize_comments=maximize_comments,
                      comment_max_pages=comment_max_pages)
        return

    # ── 仅重新采集评论模式 ──
    if args.comments_only:
        _recollect_comments_only(args.output, maximize_comments, comment_max_pages)
        return

    # 周热榜模式
    print("获取每周必看...")
    # 前置冷却，避免 B站限流
    print("  (等待 10 秒冷却...)")
    time.sleep(10)

    use_checkpoint = not args.no_checkpoint
    use_adaptive = not args.no_adaptive

    # 获取榜单元信息
    series_number = args.series
    info = None
    series_name = ""
    if series_number:
        info = get_series_info(series_number)
        if info:
            print(f"  第 {info['number']} 期: {info['name']} ({info['video_count']} 个视频)")
            series_name = info["name"]
        else:
            print(f"  第 {series_number} 期")
    else:
        print("  最新一期")

    videos = fetch_weekly_videos(series_number)

    # 未指定期号时，从期号列表取最新一期的期号（断点续传按期号匹配目录）
    if not series_number:
        try:
            series_list = fetch_weekly_series()
            if series_list:
                latest = max(series_list, key=lambda x: x["number"])
                series_number = latest["number"]
                series_name = latest.get("name", "")
                info = {"number": series_number, "name": series_name,
                        "video_count": len(videos)}
                print(f"  第 {series_number} 期: {series_name} ({len(videos)} 个视频)")
        except Exception as e:
            print(f"  ⚠️ 获取最新期号失败（本次不做断点续传匹配）: {e}")

    print(f"  获取到 {len(videos)} 个视频\n")

    # 限制处理的视频数量
    if args.number > 0:
        videos = videos[:args.number]

    # ── 智能输出目录：断点续传时复用同一期未完成的目录 ──
    output_base = _find_resumable_dir(args.output, series_number) if use_checkpoint else None
    if output_base:
        print(f"📋 断点续传模式：复用目录 {os.path.basename(output_base)}")
    else:
        output_base = os.path.join(args.output, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(output_base, exist_ok=True)

    # ── 初始化断点续传 ──
    checkpoint = None
    if use_checkpoint:
        checkpoint = CheckpointManager(
            output_base, series_number=series_number,
            series_name=series_name, total_videos=len(videos),
        )
        # 过滤已完成视频
        pending_videos = checkpoint.filter_pending(videos)
        skipped = len(videos) - len(pending_videos)
        if skipped > 0:
            print(f"📋 断点续传: {skipped} 个视频已完成，跳过")
        checkpoint.print_progress()
        videos = pending_videos

    if not videos:
        print("✅ 所有视频已处理完成！")
        return

    # ── 初始化自适应速率限制 ──
    inter_video_limiter = None
    comment_limiter = None
    danmaku_limiter = None
    if use_adaptive:
        inter_video_limiter = create_inter_video_limiter()
        comment_limiter = create_comment_limiter()
        danmaku_limiter = create_danmaku_limiter()
        print(f"🔄 自适应延迟已启用 "
              f"(视频间={inter_video_limiter.current_delay}s, "
              f"评论={comment_limiter.current_delay}s/页, "
              f"弹幕={danmaku_limiter.current_delay}s/段)")

    # ── 初始化重试队列 ──
    retry_queue = RetryQueue()

    # ── 逐个处理 ──
    all_stats = []
    for i, v in enumerate(videos, 1):
        progress = ""
        if checkpoint:
            p = checkpoint.get_progress()
            progress = f" (总进度 {p['completed']}/{p['total']})"
        print(f"[{i}/{len(videos)}]{progress}", end="")
        job = process_video(
            v, output_base, args.no_content, args.no_frames,
            checkpoint=checkpoint,
            comment_limiter=comment_limiter,
            danmaku_limiter=danmaku_limiter,
            retry_queue=retry_queue,
            maximize_comments=maximize_comments,
            comment_max_pages=comment_max_pages,
        )
        if job:
            all_stats.append(_summary_entry(job))

        # 通知限流器：有采集失败项同样视为失败，拉长后续冷却
        if inter_video_limiter:
            if job and not job.failures:
                inter_video_limiter.record_success()
            else:
                inter_video_limiter.record_failure()

        # 自适应视频间冷却
        if i < len(videos):
            if inter_video_limiter:
                delay = inter_video_limiter.current_delay
                print(f"  ⏱ 冷却 {delay:.1f}s (自适应)...")
                inter_video_limiter.wait()
            else:
                time.sleep(60)

    # ── 重试失败项 ──
    _retry_failed(retry_queue, all_stats, checkpoint,
                  comment_limiter, danmaku_limiter, inter_video_limiter)

    # ── MCP 清单生成 ──
    if not args.no_content and all_stats:
        try:
            mcp_manifest_path = generate_mcp_task_list(output_base)
            if mcp_manifest_path:
                print(f"\n📋 MCP 分析清单: {mcp_manifest_path}")
                print("  运行采集完成后可使用 mcp_integrator.py 执行画面分析")
        except Exception as e:
            print(f"\n⚠️ MCP 清单生成失败: {e}")

    # ── 跨视频对比 + 汇总报告 ──
    if len(all_stats) >= 2:
        series_info = {"number": series_number, "name": series_name} if series_number else None
        try:
            comparison, summary_path = write_summary(all_stats, output_base, series_info)
        except Exception as e:
            print(f"\n⚠️ 汇总报告生成失败: {e}")
        else:
            if len(all_stats) >= 3:
                _print_rankings(comparison)
            print(f"\n📄 汇总报告: {summary_path}")

    # ── 错误汇总 ──
    retry_queue.print_summary()
    if retry_queue.has_pending() and checkpoint:
        print("  未成功的视频已标记为失败，下次运行会自动重新采集")

    # ── 进度汇总 ──
    if checkpoint:
        checkpoint.print_progress()

    # ── 终端输出 ──
    print("\n" + "=" * 60)
    print("🏆 周热榜汇总")
    print("=" * 60)
    for item in all_stats:
        c = item["stats"].get("comments", {})
        d = item["stats"].get("danmaku", {})
        cc = item["stats"].get("comment_collection", {})
        extra = ""
        if cc:
            extra = f" | 评论采集: mode2={cc.get('mode2_count', 0)} + mode3={cc.get('mode3_count', 0)}"
        print(f"\n📺 {item['title'][:40]}")
        print(f"   评论: {c.get('total', 0)} | 弹幕: {d.get('total', 0)} | "
              f"弹幕密度: {d.get('density_per_minute', 0)}/分钟{extra}")

    print(f"\n详细结果保存在: {output_base}/")


if __name__ == "__main__":
    main()
