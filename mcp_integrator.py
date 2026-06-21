"""
MCP 内容分析桥接模块

两阶段工作流:
  Phase 1 (Python 采集):  生成 mcp_manifest.json 任务清单
  Phase 2 (Agent 分析):   读取 manifest，引导 agent 调用 MCP 工具分析图像，
                          结果回写 JSON，最后合并到 stats 和报告

CLI 用法:
  python mcp_integrator.py --generate <output_base>  生成 manifest
  python mcp_integrator.py --list <output_base>      列出待分析项
  python mcp_integrator.py --apply <output_base>     合并 MCP 结果到 stats + 重生成报告
"""
import os
import sys
import json
import argparse
from datetime import datetime

# 确保 Windows GBK 终端能输出 emoji
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

# 确保能找到模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def cmd_generate(output_base: str) -> str | None:
    """扫描输出目录生成 MCP 任务清单"""
    from content_analyzer import generate_mcp_task_list
    path = generate_mcp_task_list(output_base)
    if path:
        print(f"✅ MCP 分析清单已生成: {path}")
        # 读取并打印统计
        with open(path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        print(f"   总任务: {manifest['total_tasks']} "
              f"(待处理: {manifest['pending_tasks']}, "
              f"已完成: {manifest['completed_tasks']})")
    else:
        print("⚠️ 无待分析内容（缺少封面/帧截图或 content_prompts.json）")
    return path


def cmd_list(output_base: str):
    """列出待分析的 MCP 任务"""
    manifest_path = os.path.join(output_base, "mcp_manifest.json")
    if not os.path.isfile(manifest_path):
        print(f"⚠️ 清单不存在: {manifest_path}")
        print("  请先运行: python mcp_integrator.py --generate <output_base>")
        return

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    pending = [t for t in manifest["tasks"] if t["status"] == "pending"]
    completed = [t for t in manifest["tasks"] if t["status"] == "completed"]

    print(f"\n📋 MCP 分析清单: {manifest_path}")
    print(f"   总任务: {manifest['total_tasks']}")
    print(f"   ✅ 已完成: {len(completed)}")
    print(f"   ⏳ 待处理: {len(pending)}")

    if pending:
        print(f"\n{'─' * 60}")
        print("⏳ 待处理任务 (供 agent 执行):")
        print(f"{'─' * 60}")
        for i, task in enumerate(pending, 1):
            print(f"\n  [{i}/{len(pending)}] {task['type']}: {task['title'][:35]}")
            print(f"    task_id: {task['task_id']}")
            print(f"    image: {task['image_path']}")
            print(f"    prompt: {task['prompt'][:120]}...")
            print(f"    → 结果将写入: {task['result_path']}")

    if completed:
        print(f"\n{'─' * 60}")
        print(f"✅ 已完成任务 ({len(completed)} 项)")

    if pending:
        print(f"\n{'─' * 60}")
        print("🤖 Agent 执行指令:")
        print(f"{'─' * 60}")
        print("对于每个待处理任务，调用:")
        print('  mcp__glm-visual__analyze_image(')
        print('    image_source=task["image_path"],')
        print('    prompt=task["prompt"]')
        print('  )')
        print('然后将结果写入 task["result_path"] 对应的 JSON 文件')
        print('格式: {"task_id": "...", "analysis": "分析结果文本", "completed_at": "ISO8601"}')


def cmd_apply(output_base: str):
    """读取 MCP 分析结果，合并到 stats.json 并重新生成报告"""
    manifest_path = os.path.join(output_base, "mcp_manifest.json")
    if not os.path.isfile(manifest_path):
        print(f"⚠️ 清单不存在: {manifest_path}")
        return

    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    # 按 video_dir 分组
    from collections import defaultdict
    video_results = defaultdict(lambda: {"cover": None, "frames": []})

    for task in manifest["tasks"]:
        result_path = task.get("result_path", "")
        if not result_path or not os.path.isfile(result_path):
            continue

        try:
            with open(result_path, "r", encoding="utf-8") as f:
                result = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        if task["type"] == "cover":
            video_results[task["video_dir"]]["cover"] = result
        elif task["type"] == "frame":
            video_results[task["video_dir"]]["frames"].append({
                "time": task.get("time", ""),
                "density": task.get("density", 0),
                "analysis": result.get("analysis", ""),
            })

    # 合并到 stats.json 并重新生成报告
    from report_writer import generate_video_report

    updated = 0
    for entry_name, vr in video_results.items():
        video_dir = os.path.join(output_base, entry_name)
        stats_path = os.path.join(video_dir, "stats.json")
        if not os.path.isfile(stats_path):
            continue

        try:
            with open(stats_path, "r", encoding="utf-8") as f:
                stats = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        # 合并 MCP 结果
        if "mcp_analysis" not in stats:
            stats["mcp_analysis"] = {}
        if vr["cover"]:
            stats["mcp_analysis"]["cover"] = vr["cover"].get("analysis", "")
        if vr["frames"]:
            stats["mcp_analysis"]["frames"] = vr["frames"]

        # 保存
        with open(stats_path, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)

        # 重新生成报告（需要 title 和 content_prompts）
        title = entry_name.split("_", 1)[1] if "_" in entry_name else entry_name
        prompts_path = os.path.join(video_dir, "content_prompts.json")
        content_prompts = {}
        if os.path.isfile(prompts_path):
            try:
                with open(prompts_path, "r", encoding="utf-8") as f:
                    content_prompts = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        try:
            report_path = generate_video_report(stats, video_dir, title, content_prompts)
            if report_path:
                updated += 1
        except Exception as e:
            print(f"  ⚠️ 重新生成报告失败 [{entry_name}]: {e}")

    print(f"\n✅ MCP 结果已合并: 更新了 {updated} 个视频的报告")

    # 更新 manifest 状态
    for task in manifest["tasks"]:
        if task.get("result_path") and os.path.isfile(task["result_path"]):
            task["status"] = "completed"
    manifest["last_applied"] = datetime.now().isoformat()
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(
        description="MCP 内容分析桥接 — 管理封面/帧的 AI 分析工作流"
    )
    parser.add_argument("--generate", type=str, default=None, metavar="OUTPUT_BASE",
                        help="扫描输出目录，生成 mcp_manifest.json")
    parser.add_argument("--list", type=str, default=None, metavar="OUTPUT_BASE",
                        help="列出待分析的 MCP 任务")
    parser.add_argument("--apply", type=str, default=None, metavar="OUTPUT_BASE",
                        help="读取 MCP 分析结果，合并到 stats 并重生成报告")
    args = parser.parse_args()

    if args.generate:
        cmd_generate(args.generate)
    elif args.list:
        cmd_list(args.list)
    elif args.apply:
        cmd_apply(args.apply)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
