"""
B站周热榜评论 & 弹幕统计分析 - 主入口
"""
import os
import sys
import json
import time
from datetime import datetime

# 确保 Windows GBK 终端能输出 emoji
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

# 确保能找到模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ranking import fetch_weekly_videos, fetch_weekly_series, get_series_info
from comments import fetch_comments, fetch_comments_maximized
from danmaku import fetch_video_danmaku
from stats import build_statistics, print_report, save_results, build_cross_video_comparison
from content_analyzer import (
    fetch_hardware_params, download_cover, capture_frames, build_content_prompts,
    generate_mcp_task_list,
)
from report_writer import generate_video_report, generate_summary_report
from checkpoint_manager import CheckpointManager
from adaptive_retry import (
    AdaptiveRateLimiter, RetryQueue,
    create_comment_limiter, create_danmaku_limiter, create_inter_video_limiter,
)


def process_video(video, output_base: str, no_content: bool = False,
                  no_frames: bool = False, checkpoint: CheckpointManager = None,
                  comment_limiter: AdaptiveRateLimiter = None,
                  danmaku_limiter: AdaptiveRateLimiter = None,
                  retry_queue: RetryQueue = None,
                  maximize_comments: bool = True):
    """处理单个视频：采集评论 + 弹幕 + 统计 + 内容分析"""
    aid = video.aid if hasattr(video, "aid") else video["aid"]
    cid = video.cid if hasattr(video, "cid") else video.get("cid", 0)
    bvid = video.bvid if hasattr(video, "bvid") else video.get("bvid", "")
    title = video.title if hasattr(video, "title") else video["title"]
    duration = video.duration if hasattr(video, "duration") else video.get("duration", 0)
    pic_url = video.pic if hasattr(video, "pic") else video.get("pic", "")

    # 清理文件名
    safe_title = "".join(c for c in title if c.isalnum() or c in " _-（）()【】")[:40]
    output_dir = os.path.join(output_base, f"{aid}_{safe_title}")

    print(f"\n{'─' * 50}")
    print(f"🎬 {title}")
    print(f"   aid={aid}, cid={cid}, 时长={duration // 60}分{duration % 60}秒")
    print(f"{'─' * 50}")

    # ── 内容分析：硬参数采集 ──
    video_specs = {}
    if not no_content:
        print("  采集硬参数...", end=" ")
        try:
            video_specs = fetch_hardware_params(aid)
            print(f"✓ {video_specs.get('width', '?')}x{video_specs.get('height', '?')} "
                  f"{video_specs.get('duration_seconds', '?')}s")
            # 从硬参数补充缺失字段
            if not bvid and video_specs.get("bvid"):
                bvid = video_specs["bvid"]
            if not pic_url and video_specs.get("pic_url"):
                pic_url = video_specs["pic_url"]
        except Exception as e:
            print(f"✗ {e}")

    # ── 内容分析：封面下载 ──
    cover_path = None
    if not no_content and pic_url:
        print("  下载封面...", end=" ")
        cover_path = download_cover(pic_url, os.path.join(output_dir, "cover.jpg"))
        if cover_path:
            print("✓")
        else:
            print("✗ 下载失败")

    # 采集评论
    comment_stats_info = None
    if maximize_comments:
        print("  采集评论(双模式最大化)...", end=" ")
        try:
            comments, comment_total, comment_stats_info = fetch_comments_maximized(aid)
            print(f"✓ {len(comments)} 条主评论 (去重后) / {comment_total} 总计")
            if comment_stats_info:
                print(f"    mode2={comment_stats_info['mode2_count']} "
                      f"mode3={comment_stats_info['mode3_count']} "
                      f"重叠{comment_stats_info['overlap_count']}条 "
                      f"新增{comment_stats_info['mode3_count'] - comment_stats_info['overlap_count']}条")
        except Exception as e:
            print(f"✗ {e}")
            if retry_queue:
                retry_queue.add(aid, title, "comments", str(e),
                                {"maximize": True})
            comments = []
    else:
        print("  采集评论...", end=" ")
        try:
            comments, comment_total = fetch_comments(aid)
            print(f"✓ {len(comments)} 条主评论 / {comment_total} 总计")
        except Exception as e:
            print(f"✗ {e}")
            if retry_queue:
                retry_queue.add(aid, title, "comments", str(e))
            comments = []

    # 采集弹幕
    if cid and duration > 0:
        print("  采集弹幕...", end=" ")
        try:
            delay = danmaku_limiter.current_delay if danmaku_limiter else 0.6
            danmaku = fetch_video_danmaku(cid, duration, delay=delay)
            print(f"✓ {len(danmaku)} 条弹幕")
        except Exception as e:
            print(f"✗ {e}")
            if retry_queue:
                retry_queue.add(aid, title, "danmaku", str(e),
                                {"cid": cid, "duration": duration})
            danmaku = []
    else:
        print("  跳过弹幕 (无 cid 或时长为0)")
        danmaku = []

    if not comments and not danmaku and not video_specs:
        print("  ⚠️ 无数据，跳过")
        if checkpoint:
            checkpoint.mark_video_failed(aid, "无数据：评论、弹幕、视频规格均为空", title)
        return None

    # 统计分析
    owner_mid = video.owner_mid if hasattr(video, "owner_mid") else video.get("owner_mid", 0)
    stats = build_statistics(comments, danmaku, video_specs=video_specs, owner_mid=owner_mid)
    # 附加评论采集统计
    if comment_stats_info:
        stats["comment_collection"] = comment_stats_info

    # ── 内容分析：弹幕高潮帧截取 ──
    frame_results = []
    if not no_content and not no_frames and bvid and danmaku:
        peaks = stats.get("danmaku_peaks", [])
        if peaks:
            top_peaks = peaks[:3]  # Top 3 高潮时刻
            print(f"  截取 {len(top_peaks)} 个高潮帧...", end=" ")
            try:
                frame_dir = os.path.join(output_dir, "frames")
                frame_results = capture_frames(
                    bvid, top_peaks, frame_dir, cid=cid, aid=aid,
                    duration_seconds=duration,
                )
                print(f"✓ 成功 {len(frame_results)}/{len(top_peaks)}")
            except Exception as e:
                print(f"✗ {e}")

    # ── 内容分析：生成 MCP 分析提示词 ──
    content_prompts = {}
    if not no_content:
        content_prompts = build_content_prompts(cover_path, frame_results, title)
        # 保存提示词供后续 MCP 分析
        if content_prompts:
            import json
            with open(os.path.join(output_dir, "content_prompts.json"), "w", encoding="utf-8") as f:
                json.dump(content_prompts, f, ensure_ascii=False, indent=2)
        stats["video_specs"] = video_specs
        stats["cover_path"] = cover_path
        stats["frame_results"] = frame_results

    print_report(stats, title)
    save_results(comments, danmaku, stats, output_dir, title)

    # ── 生成 Markdown 报告 ──
    report_path = None
    try:
        report_path = generate_video_report(stats, output_dir, title, content_prompts)
        if report_path:
            print(f"  📄 报告: {report_path}")
    except Exception as e:
        print(f"  ⚠️ 报告生成失败: {e}")

    # 标记完成
    if checkpoint:
        stats_summary = {
            "comments": stats.get("comments", {}).get("total", 0),
            "danmaku": stats.get("danmaku", {}).get("total", 0),
        }
        checkpoint.mark_video_complete(aid, f"{aid}_{safe_title}",
                                       stats_summary, title)

    return {
        "comments": comments, "danmaku": danmaku, "stats": stats,
        "content_prompts": content_prompts, "report_path": report_path,
    }


def _find_resumable_dir(output_root: str, series_number: int) -> str | None:
    """查找可复用的断点续传目录

    优先匹配相同系列号且已完成最多的目录。
    """
    if not os.path.isdir(output_root):
        return None

    best_match = None
    best_completed = -1
    fallback = None
    fallback_completed = -1

    for entry in sorted(os.listdir(output_root), reverse=True):
        entry_path = os.path.join(output_root, entry)
        if not os.path.isdir(entry_path):
            continue

        manifest_path = os.path.join(entry_path, "resume_state.json")
        if not os.path.isfile(manifest_path):
            continue

        try:
            with open(manifest_path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        videos = state.get("videos", {})
        completed = sum(1 for v in videos.values() if v["state"] == "completed")
        total = state.get("total_videos", 0)

        # 所有视频都已处理完成 → 跳过
        if total > 0 and completed >= total:
            continue
        if completed > 0 and total == 0:
            continue

        same_series = series_number and state.get("series_number") == series_number

        if same_series and completed > best_completed:
            best_completed = completed
            best_match = entry_path
        elif not same_series and completed > fallback_completed:
            fallback_completed = completed
            fallback = entry_path

    # 优先返回同系列号目录，否则返回最新不完整目录
    return best_match or fallback


def _recollect_comments_only(output_root: str, maximize: bool = True):
    """仅重新采集已完成视频的评论，更新 stats 和报告"""
    from comments import fetch_comments_maximized, fetch_comments
    from stats import build_statistics, print_report, save_results
    from report_writer import generate_video_report

    # 找到最新的运行目录
    run_dir = _find_resumable_dir(output_root, None)
    if not run_dir:
        # 回退：找最新的有子目录的
        if os.path.isdir(output_root):
            dirs = sorted(
                [d for d in os.listdir(output_root)
                 if os.path.isdir(os.path.join(output_root, d))],
                reverse=True,
            )
            for d in dirs:
                candidate = os.path.join(output_root, d)
                if os.path.isfile(os.path.join(candidate, "resume_state.json")):
                    run_dir = candidate
                    break
            if not run_dir:
                print("错误: 找不到可用的运行目录")
                return
        else:
            print("错误: 找不到可用的运行目录")
            return

    print(f"📋 评论重采模式: {os.path.basename(run_dir)}")

    # 读取 manifest 获取已完成视频
    manifest_path = os.path.join(run_dir, "resume_state.json")
    if not os.path.isfile(manifest_path):
        print("错误: 无断点续传清单")
        return

    with open(manifest_path, "r", encoding="utf-8") as f:
        state = json.load(f)

    videos = state.get("videos", {})
    completed = [
        (int(aid), entry["title"])
        for aid, entry in videos.items()
        if entry["state"] == "completed"
    ]

    if not completed:
        print("无已完成视频可重新采集")
        return

    print(f"  共 {len(completed)} 个视频需要重新采集评论\n")

    for i, (aid, title) in enumerate(completed, 1):
        # 找到对应的输出目录
        video_dir = None
        for entry in os.listdir(run_dir):
            if entry.startswith(f"{aid}_"):
                video_dir = os.path.join(run_dir, entry)
                break
        if not video_dir:
            print(f"[{i}/{len(completed)}] {title[:30]} - ⚠️ 目录不存在，跳过")
            continue

        print(f"[{i}/{len(completed)}] {title[:40]}")

        # 重新采集评论
        if maximize:
            print("  采集评论(双模式)...", end=" ")
            try:
                comments, _, _ = fetch_comments_maximized(aid)
            except Exception as e:
                print(f"✗ {e}")
                comments = []
        else:
            try:
                comments, _ = fetch_comments(aid)
            except Exception as e:
                print(f"✗ {e}")
                comments = []
        print(f"✓ {len(comments)} 条")

        # 读取现有 stats
        stats_path = os.path.join(video_dir, "stats.json")
        if os.path.isfile(stats_path):
            with open(stats_path, "r", encoding="utf-8") as f:
                stats = json.load(f)
        else:
            stats = {}

        # 读取现有 danmaku（JSON → Dataclass 对象）
        danmaku_path = os.path.join(video_dir, "danmaku.json")
        danmaku = []
        if os.path.isfile(danmaku_path):
            with open(danmaku_path, "r", encoding="utf-8") as f:
                raw_danmaku = json.load(f)
            from danmaku import Danmaku
            danmaku = [
                Danmaku(
                    id=d.get("id", 0),
                    progress=d.get("progress", 0),
                    mode=d.get("mode", 0),
                    fontsize=d.get("fontsize", 25),
                    color=d.get("color", 0xFFFFFF),
                    mid_hash=d.get("mid_hash", ""),
                    content=d.get("content", ""),
                    ctime=d.get("ctime", 0),
                    weight=d.get("weight", 0),
                    pool=d.get("pool", 0),
                )
                for d in raw_danmaku
                if isinstance(d, dict)
            ]

        # 重新统计分析
        video_specs = stats.get("video_specs", {})
        owner_mid = stats.get("owner_mid", 0)
        new_stats = build_statistics(comments, danmaku,
                                     video_specs=video_specs,
                                     owner_mid=owner_mid)
        # 保留 MCP 分析结果
        if "mcp_analysis" in stats:
            new_stats["mcp_analysis"] = stats["mcp_analysis"]
        if "video_specs" in stats:
            new_stats["video_specs"] = stats["video_specs"]
        if "cover_path" in stats:
            new_stats["cover_path"] = stats["cover_path"]
        if "frame_results" in stats:
            new_stats["frame_results"] = stats["frame_results"]

        print_report(new_stats, title)
        save_results(comments, danmaku, new_stats, video_dir, title)

        # 重新生成报告
        prompts_path = os.path.join(video_dir, "content_prompts.json")
        content_prompts = {}
        if os.path.isfile(prompts_path):
            with open(prompts_path, "r", encoding="utf-8") as f:
                content_prompts = json.load(f)

        try:
            report_path = generate_video_report(new_stats, video_dir, title,
                                                content_prompts)
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
    for aid, title in completed:
        for entry in os.listdir(run_dir):
            if entry.startswith(f"{aid}_"):
                d = os.path.join(run_dir, entry)
                sp = os.path.join(d, "stats.json")
                if os.path.isfile(sp):
                    with open(sp, "r", encoding="utf-8") as f:
                        s = json.load(f)
                    all_stats_objs.append({
                        "aid": aid, "title": title,
                        "views": 0, "likes": 0, "stats": s,
                    })
                break

    if len(all_stats_objs) >= 2:
        series_num = state.get("series_number")
        series_nm = state.get("series_name", "")
        si = {"number": series_num, "name": series_nm} if series_num else None
        from report_writer import generate_summary_report
        from stats import build_cross_video_comparison
        comparison = build_cross_video_comparison(all_stats_objs)
        with open(os.path.join(run_dir, "summary.json"), "w",
                  encoding="utf-8") as f:
            json.dump(comparison, f, ensure_ascii=False, indent=2)
        try:
            spath = generate_summary_report(all_stats_objs, run_dir, si)
            if spath:
                print(f"  📄 汇总报告已更新: {spath}")
        except Exception as e:
            print(f"  ⚠️ 汇总报告失败: {e}")

    print("\n✅ 评论重采完成!")


def main():
    import argparse
    import json
    parser = argparse.ArgumentParser(description="B站周热榜评论弹幕统计分析")
    parser.add_argument("-n", "--number", type=int, default=None,
                        help="指定周热榜期号，不指定则获取最新一期")
    parser.add_argument("-l", "--limit", type=int, default=0,
                        help="采集视频数量上限 (默认0=全部)")
    parser.add_argument("-o", "--output", type=str, default="./bilibili_output",
                        help="输出目录 (默认 ./bilibili_output)")
    parser.add_argument("--list", action="store_true",
                        help="仅列出现有期号列表")
    parser.add_argument("--aid", type=int, default=None,
                        help="直接指定视频 aid（跳过周热榜）")
    parser.add_argument("--cid", type=int, default=None,
                        help="直接指定视频 cid（配合 --aid 使用）")
    parser.add_argument("--duration", type=int, default=0,
                        help="视频时长（秒，配合 --aid 使用）")
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
    args = parser.parse_args()

    # 列出期号
    if args.list:
        print("获取每周必看期号列表...")
        series_list = fetch_weekly_series()
        for s in sorted(series_list, key=lambda x: x["number"], reverse=True)[:20]:
            print(f"  第 {s['number']} 期: {s['name']} ({s.get('subject', '')})")
        return

    output_base = os.path.join(args.output, datetime.now().strftime("%Y%m%d_%H%M%S"))

    # 直接采集模式
    if args.aid:
        from ranking import VideoInfo
        video = VideoInfo(
            aid=args.aid, cid=args.cid or 0, bvid="", title=args.title or "手动指定",
            owner_name="", owner_mid=0, view=0, danmaku=0, reply=0,
            favorite=0, coin=0, share=0, like=0,
            duration=args.duration, width=0, height=0, pic="", pubdate=0,
        )
        process_video(video, output_base, args.no_content, args.no_frames)
        return

    # ── 仅重新采集评论模式 ──
    if args.comments_only:
        _recollect_comments_only(args.output, not args.no_maximize_comments)
        return

    # 周热榜模式
    print("获取每周必看...")
    # 前置冷却，避免 B站限流
    print("  (等待 10 秒冷却...)")
    time.sleep(10)

    use_checkpoint = not args.no_checkpoint
    use_adaptive = not args.no_adaptive
    maximize_comments = not args.no_maximize_comments

    # 获取榜单元信息
    series_number = args.number
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

    # 获取最新一期的期号
    if info is None and not series_number:
        # 从视频列表反推期号：尝试获取最新期信息
        try:
            # 获取期号列表，最新的是第一个
            series_list = fetch_weekly_series()
            if series_list:
                latest = sorted(series_list, key=lambda x: x["number"], reverse=True)[0]
                series_number = latest["number"]
                series_name = latest.get("name", "")
                info = {"number": series_number, "name": series_name,
                        "video_count": len(videos)}
                print(f"  第 {series_number} 期: {series_name} ({len(videos)} 个视频)")
        except Exception:
            pass

    print(f"  获取到 {len(videos)} 个视频\n")

    # ── 智能输出目录：断点续传时复用已有目录 ──
    if use_checkpoint:
        # 查找是否有可复用的运行目录
        output_base = _find_resumable_dir(args.output, series_number)
        if output_base:
            print(f"📋 断点续传模式：复用目录 {os.path.basename(output_base)}")
        else:
            output_base = os.path.join(args.output, datetime.now().strftime("%Y%m%d_%H%M%S"))
    else:
        output_base = os.path.join(args.output, datetime.now().strftime("%Y%m%d_%H%M%S"))
    os.makedirs(output_base, exist_ok=True)

    # 限制数量（0 表示不限制）
    if args.limit > 0:
        videos = videos[:args.limit]

    # 获取系列信息用于 checkpoint
    if info is None and series_number:
        info = get_series_info(series_number)

    # ── 初始化断点续传 ──
    checkpoint = None
    if use_checkpoint:
        series_name = info["name"] if info else ""
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
        if checkpoint:
            checkpoint.print_progress()
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
        total = len(videos)
        if checkpoint:
            p = checkpoint.get_progress()
            total = p["total"]
        print(f"[{i}/{total}]", end="")
        result = process_video(
            v, output_base, args.no_content, args.no_frames,
            checkpoint=checkpoint,
            comment_limiter=comment_limiter,
            danmaku_limiter=danmaku_limiter,
            retry_queue=retry_queue,
            maximize_comments=maximize_comments,
        )
        if result:
            all_stats.append({
                "aid": v.aid,
                "title": v.title,
                "views": v.view,
                "likes": v.like,
                "stats": result["stats"],
            })
            # 成功时通知限流器
            if inter_video_limiter:
                inter_video_limiter.record_success()
        else:
            # 失败时通知限流器
            if inter_video_limiter:
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
    if retry_queue.has_pending():
        print(f"\n{'─' * 50}")
        print(f"🔄 重试失败项 ({retry_queue.get_pending_count()} 项)")
        print(f"{'─' * 50}")
        for item in retry_queue.get_pending():
            print(f"  重试 [{item.operation}] {item.title[:30]} (aid={item.aid})...")
            try:
                if item.operation == "comments":
                    if item.context.get("maximize"):
                        comments, _, _ = fetch_comments_maximized(item.aid)
                    else:
                        comments, _ = fetch_comments(item.aid)
                    retry_queue.mark_retried(item, True)
                    print(f"    ✓ 采集到 {len(comments)} 条评论")
                elif item.operation == "danmaku":
                    danmaku = fetch_video_danmaku(
                        item.context.get("cid", 0),
                        item.context.get("duration", 0),
                    )
                    retry_queue.mark_retried(item, True)
                    print(f"    ✓ 采集到 {len(danmaku)} 条弹幕")
                else:
                    retry_queue.mark_retried(item, False, "不支持的重试操作")
            except Exception as e:
                retry_queue.mark_retried(item, False, str(e))
                print(f"    ✗ {e}")

    # ── MCP 清单生成 ──
    mcp_manifest_path = None
    if not args.no_content and all_stats:
        try:
            mcp_manifest_path = generate_mcp_task_list(output_base)
            if mcp_manifest_path:
                print(f"\n📋 MCP 分析清单: {mcp_manifest_path}")
                print("  运行采集完成后可使用 mcp_integrator.py 执行画面分析")
        except Exception as e:
            print(f"\n⚠️ MCP 清单生成失败: {e}")

    # ── 跨视频横向对比 ──
    if len(all_stats) >= 3:
        print("\n" + "=" * 60)
        print("🏆 跨视频横向对比")
        print("=" * 60)
        comparison = build_cross_video_comparison(all_stats)

        # 弹幕密度 TOP 5
        rankings = comparison.get("rankings", {})
        if "danmaku_density" in rankings:
            print("\n📊 弹幕密度排行 TOP 5:")
            for item in rankings["danmaku_density"][:5]:
                print(f"  #{item['rank']} {item['title'][:35]} - {item['value']}{item['unit']}")

        # UP主互动率 TOP 5
        if "up_interaction" in rankings:
            print("\n💬 UP主互动率排行 TOP 5:")
            for item in rankings["up_interaction"][:5]:
                print(f"  #{item['rank']} {item['title'][:35]} - {item['value']}{item['unit']}")

        # 正面评论率 TOP 5
        if "positive_sentiment" in rankings:
            print("\n😊 正面评论率排行 TOP 5:")
            for item in rankings["positive_sentiment"][:5]:
                print(f"  #{item['rank']} {item['title'][:35]} - {item['value']}{item['unit']}")

        # 综合评分
        overall = comparison.get("overall_scores", [])
        if overall:
            print("\n🌟 综合评分 TOP 10:")
            for item in overall[:10]:
                print(f"  #{item['rank']} {item['title'][:35]} - {item['overall_score']}/100")

        # 保存汇总
        import json
        with open(os.path.join(output_base, "summary.json"), "w", encoding="utf-8") as f:
            json.dump(comparison, f, ensure_ascii=False, indent=2)

    # ── 汇总报告 ──
    if len(all_stats) >= 2:
        series_info = None
        if args.number or series_number:
            series_info = {
                "number": args.number or series_number,
                "name": info.get("name", "") if info else "",
            }
        try:
            summary_path = generate_summary_report(all_stats, output_base, series_info)
            if summary_path:
                print(f"\n📄 汇总报告: {summary_path}")
        except Exception as e:
            print(f"\n⚠️ 汇总报告生成失败: {e}")

    # ── 错误汇总 ──
    retry_queue.print_summary()

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
            extra = f" | 评论采集: mode2={cc.get('mode2_count',0)} + mode3={cc.get('mode3_count',0)}"
        print(f"\n📺 {item['title'][:40]}")
        print(f"   评论: {c.get('total', 0)} | 弹幕: {d.get('total', 0)} | "
              f"弹幕密度: {d.get('density_per_minute', 0)}/分钟{extra}")

    print(f"\n详细结果保存在: {output_base}/")


if __name__ == "__main__":
    main()
