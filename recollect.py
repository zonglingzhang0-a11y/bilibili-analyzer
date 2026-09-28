"""
评论重采：对一次运行中已完成的视频重新采集评论（例如登录恢复后补全评论、改为全量采集），
更新 stats、单视频报告、汇总报告和周报。支持中断后继续。
"""
import json
import os
import time
from datetime import datetime

from adaptive_retry import create_comment_limiter
from content_analyzer import fetch_hardware_params
from danmaku import danmaku_from_dict
from pipeline import (
    DEFAULT_COMMENT_PAGES, MAX_CONSECUTIVE_FAILURES, comment_strategy, fetch_video_comments,
    write_weekly_report,
)
from rebuild import PRESERVED_KEYS
from report_writer import generate_video_report, write_summary
from runs import (
    find_video_dir, latest_run_dir, load_recollect_state, load_resume_state, load_summary_entries,
    save_recollect_state,
)
from stats import build_statistics, print_report, save_results


def recollect_comments(output_root: str, maximize: bool = True,
                       comment_max_pages: int = DEFAULT_COMMENT_PAGES,
                       full: bool = False, run_name: str = None):
    """仅重新采集最新一次运行中已完成视频的评论，更新 stats 和报告

    支持中断后继续：每个视频采完就记录到运行目录的 comments_recollect.json，
    重新运行同一命令时跳过已按相同方式（或全量）采过的视频。
    """
    run_dir = os.path.join(output_root, run_name) if run_name else latest_run_dir(output_root)
    if not run_dir or load_resume_state(run_dir) is None:
        print("错误: 找不到可用的运行目录（需包含 resume_state.json）")
        return

    strategy = comment_strategy(full, maximize, comment_max_pages)
    print(f"📋 评论重采模式: {os.path.basename(run_dir)}（策略 {strategy}）")
    state = load_resume_state(run_dir) or {}
    completed = [
        (int(aid), entry.get("title", ""), entry.get("output_dir", ""))
        for aid, entry in state.get("videos", {}).items()
        if entry.get("state") == "completed"
    ]
    if not completed:
        print("无已完成视频可重新采集")
        return

    progress = load_recollect_state(run_dir)
    limiter = create_comment_limiter()
    done, skipped, failed = 0, 0, []
    consecutive_failures = 0
    paused = False
    print(f"  共 {len(completed)} 个视频需要重新采集评论\n")

    for i, (aid, title, dir_name) in enumerate(completed, 1):
        record = progress.get(str(aid), {})
        if record.get("strategy") in (strategy, "full"):
            print(f"[{i}/{len(completed)}] {title[:40]} - 已按{record['strategy']}采集过"
                  f"（{record.get('count', 0)} 条），跳过")
            skipped += 1
            continue

        video_dir = find_video_dir(run_dir, aid, dir_name)
        if not video_dir:
            print(f"[{i}/{len(completed)}] {title[:30]} - ⚠️ 目录不存在，跳过")
            continue

        print(f"[{i}/{len(completed)}] {title[:40]}", flush=True)

        # 读取现有 stats
        stats_path = os.path.join(video_dir, "stats.json")
        stats = {}
        if os.path.isfile(stats_path):
            with open(stats_path, "r", encoding="utf-8") as f:
                stats = json.load(f)
        old_comment_total = stats.get("comments", {}).get("total", 0)

        # 重新采集评论；失败或一条没拿到时保留原数据，避免覆盖成空
        try:
            comments, reported_total, comment_stats_info = fetch_video_comments(
                aid, full=full, maximize=maximize, max_pages=comment_max_pages, limiter=limiter)
        except Exception as e:
            error = f"{e}，保留原有数据"
        else:
            error = None
            if not comments and old_comment_total > 0:
                error = f"未采集到评论（原有 {old_comment_total} 条），保留原有数据"
        if error:
            print(f"  ✗ {error}")
            failed.append(title)
            consecutive_failures += 1
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"\n⛔ 连续 {consecutive_failures} 个视频采集失败，可能是网络异常或被限流，已暂停。"
                      "\n   请确认网络正常后重新运行同一命令，已完成的视频会自动跳过。", flush=True)
                paused = True
                break
            if i < len(completed):
                print("  ⏱ 冷却 5 分钟后继续下一个视频...", flush=True)
                time.sleep(300)
            continue
        consecutive_failures = 0

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

        progress[str(aid)] = {
            "strategy": strategy,
            "count": len(comments),
            "reported_total": reported_total,
            "finished_at": datetime.now().isoformat(timespec="seconds"),
        }
        save_recollect_state(run_dir, progress)
        done += 1

        # 视频间冷却
        if i < len(completed):
            time.sleep(15)

    # 重新生成汇总
    print("\n重新生成汇总报告...")
    all_stats_objs = load_summary_entries(run_dir, completed)

    if len(all_stats_objs) >= 2:
        series_num = state.get("series_number")
        si = {"number": series_num, "name": state.get("series_name", "")} if series_num else None
        try:
            _, spath = write_summary(all_stats_objs, run_dir, si)
            print(f"  📄 汇总报告已更新: {spath}")
        except Exception as e:
            print(f"  ⚠️ 汇总报告失败: {e}")
        write_weekly_report(run_dir)

    print(f"\n本次完成 {done} 个，之前已完成跳过 {skipped} 个，失败 {len(failed)} 个"
          + ("（已暂停，未处理完）" if paused else ""))
    if failed:
        print("  失败的视频保留了原有数据，重新运行同一命令会继续采集它们：")
        for title in failed:
            print(f"    - {title[:50]}")
    else:
        print("✅ 评论重采完成!")
