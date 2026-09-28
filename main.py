"""
B站周热榜评论 & 弹幕统计分析 - 命令行入口

具体流程见 pipeline.py（采集）、recollect.py（评论重采）、rebuild.py（离线重建）。
"""
import argparse
import os
import sys
from datetime import datetime

# 确保 Windows GBK 终端能输出 emoji
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")
if sys.stderr.encoding != "utf-8":
    sys.stderr.reconfigure(encoding="utf-8")

# 确保能找到模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from comments import check_login
from pipeline import DEFAULT_COMMENT_PAGES, RunOptions, process_video, run_series
from ranking import VideoInfo, fetch_weekly_series
from rebuild import rebuild_all
from recollect import recollect_comments
from runs import RUN_START_MARKER


def _check_login(required: bool) -> bool:
    """检查登录状态。未登录时每个视频每种排序只能拿到约 3 条评论

    Args:
        required: 为 True 时（全量采集、评论重采）未登录即拒绝运行，避免产生残缺数据

    Returns:
        是否可以继续运行
    """
    status = check_login()
    if status is True:
        return True
    if status is False:
        print("⚠️ 登录凭证 SESSDATA 已失效（当前为未登录状态）")
    else:
        print("⚠️ 未配置登录凭证 SESSDATA（或登录状态检查失败）")
    print("   未登录时每个视频每种排序只能采到约 3 条评论，评论数据会严重不全。")
    print("   可运行 python bili_auth.py --login 扫码登录（自动更新 .sessdata），"
          "或手动更新项目根目录的 .sessdata")
    if required:
        print("✗ 全量采集 / 评论重采需要有效的登录状态，已停止运行\n")
        return False
    print()
    return True


class _Tee:
    """把输出同时写到终端和日志文件"""

    def __init__(self, stream, log_file):
        self._stream = stream
        self._log_file = log_file

    def write(self, data):
        self._stream.write(data)
        self._log_file.write(data)
        return len(data)

    def flush(self):
        self._stream.flush()
        self._log_file.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


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
    parser.add_argument("--full-comments", action="store_true",
                        help="全量采集一级评论：按时间排序翻到最后一页（忽略 -l 和双模式），耗时很长")
    parser.add_argument("--comments-only", action="store_true",
                        help="仅重新采集已处理视频的评论（跳过弹幕/内容分析）")
    parser.add_argument("--rebuild", nargs="?", const="", default=None, metavar="RUN_DIR",
                        help="离线重建：用已保存的评论/弹幕重新统计并生成报告，不联网。"
                             "指定运行目录则只重建该目录，否则重建 -o 下全部")
    parser.add_argument("--log-file", type=str, default=None, metavar="PATH",
                        help="输出同时追加写入该日志文件（UTF-8），便于长时间运行时事后查看")
    parser.add_argument("--run-name", type=str, default=None, metavar="NAME",
                        help="指定运行目录名（位于 -o 下，如 20260911_180000）；目录已存在时接着其中的进度继续。"
                             "配合 --comments-only 时表示重采该目录")
    args = parser.parse_args()

    if args.run_name and (os.path.basename(args.run_name) != args.run_name
                          or args.run_name in (".", "..")):
        parser.error("--run-name 只能是目录名，不能包含路径")

    if args.log_file:
        os.makedirs(os.path.dirname(os.path.abspath(args.log_file)), exist_ok=True)
        log_file = open(args.log_file, "a", encoding="utf-8", buffering=1)
        sys.stdout = _Tee(sys.stdout, log_file)
        sys.stderr = _Tee(sys.stderr, log_file)
        # 日志是追加写入的，用这一行标记每次运行的起点（进度看板据此计算本次运行的速度）
        print(f"{RUN_START_MARKER} {datetime.now():%Y-%m-%d %H:%M:%S}  参数: {' '.join(sys.argv[1:])}")

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

    if not _check_login(required=args.full_comments or args.comments_only):
        return

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
                      comment_max_pages=comment_max_pages,
                      full_comments=args.full_comments)
        return

    # ── 仅重新采集评论模式 ──
    if args.comments_only:
        recollect_comments(args.output, maximize_comments, comment_max_pages,
                           full=args.full_comments, run_name=args.run_name)
        return

    # 周热榜模式
    run_series(RunOptions(
        output=args.output, series=args.series, number=args.number, run_name=args.run_name,
        no_content=args.no_content, no_frames=args.no_frames,
        use_checkpoint=not args.no_checkpoint, use_adaptive=not args.no_adaptive,
        maximize_comments=maximize_comments, comment_max_pages=comment_max_pages,
        full_comments=args.full_comments,
    ))


if __name__ == "__main__":
    main()
