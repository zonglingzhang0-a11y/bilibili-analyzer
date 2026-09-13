"""
B站评论采集模块
使用 pn/ps 分页，20条/页
使用完整浏览器 headers + Cookie 会话避免 412 封禁
"""
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from bili_auth import DEFAULT_SESSDATA_FILE, load_sessdata
from bili_http import BROWSER_HEADERS, make_client


@dataclass
class CommentReply:
    """子回复"""
    rpid: int
    mid: int
    member_name: str
    content: str
    ctime: int
    like: int
    parent_rpid: int


@dataclass
class Comment:
    """主评论"""
    rpid: int
    oid: int
    mid: int
    member_name: str
    content: str
    ctime: int
    like: int
    rcount: int
    replies: list[CommentReply] = field(default_factory=list)
    up_replied: bool = False  # 接口 up_action.reply：UP主是否回复过该评论


# 全量采集时的翻页上限（每页 20 条，足够覆盖任何视频），只作为防止死循环的保护
FULL_MAX_PAGES = 100_000
# 网络出错（超时、SSL 断连等）时同一页的最大重试次数，等待 15s 起翻倍、最长 240s，合计约 12 分钟
MAX_NETWORK_RETRIES = 6


class CommentsIncomplete(RuntimeError):
    """评论没有采完（限流重试耗尽或中途接口出错）"""

    def __init__(self, message: str, collected: int):
        super().__init__(f"{message}（已采 {collected} 条）")
        self.collected = collected


def _parse_ctime(value) -> int:
    """comments.json 中的 ctime 是 ISO 时间字符串（空串表示未知），还原为时间戳"""
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).timestamp()) if value else 0
        except ValueError:
            return 0
    return int(value or 0)


def comment_from_dict(d: dict, oid: int = 0) -> Comment:
    """从 save_results 写出的 comments.json 条目还原 Comment

    兼容旧格式：旧版本没有保存 mid / up_replied / 子回复的 rpid、mid、ctime，缺失时取默认值。
    """
    rpid = d.get("rpid", 0)
    return Comment(
        rpid=rpid,
        oid=d.get("oid", oid),
        mid=d.get("mid", 0),
        member_name=d.get("member_name", ""),
        content=d.get("content", ""),
        ctime=_parse_ctime(d.get("ctime")),
        like=d.get("like", 0),
        rcount=d.get("rcount", 0),
        replies=[
            CommentReply(
                rpid=r.get("rpid", 0),
                mid=r.get("mid", 0),
                member_name=r.get("member_name", ""),
                content=r.get("content", ""),
                ctime=_parse_ctime(r.get("ctime")),
                like=r.get("like", 0),
                parent_rpid=rpid,
            )
            for r in d.get("replies") or []
            if isinstance(r, dict)
        ],
        up_replied=bool(d.get("up_replied", False)),
    )


def _build_session() -> httpx.Client:
    """创建带 Cookie 的 httpx 会话，模拟正常浏览器访问"""
    client = make_client(BROWSER_HEADERS, timeout=20, follow_redirects=True)

    # 先访问 B站首页获取初始 Cookie
    try:
        resp = client.get("https://www.bilibili.com/")
    except Exception:
        pass

    # 读取登录 Cookie：优先环境变量 BILI_SESSDATA，回退项目根目录 .sessdata 文件
    sessdata = load_sessdata(DEFAULT_SESSDATA_FILE, required=False)
    if sessdata:
        client.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")

    # 确保有 buvid3（B站设备标识）
    if "buvid3" not in client.cookies:
        client.cookies.set("buvid3", "auto", domain=".bilibili.com")

    return client


def check_login() -> bool | None:
    """检查配置的 SESSDATA 是否仍处于登录状态

    未登录会话（包括 SESSDATA 过期）调用评论接口时，每种排序总共只返回约 3 条评论。

    Returns:
        True 已登录；False 配置了 SESSDATA 但未登录（已失效）；None 未配置或检查失败
    """
    if not load_sessdata(DEFAULT_SESSDATA_FILE, required=False):
        return None
    # _build_session 内部已发过请求，不能再用 with 打开，手动关闭
    client = _build_session()
    try:
        data = client.get("https://api.bilibili.com/x/web-interface/nav").json()
        return bool((data.get("data") or {}).get("isLogin"))
    except Exception:
        return None
    finally:
        client.close()


def _try_sign_params(params: dict) -> dict:
    """尝试对评论请求参数进行 WBI 签名"""
    try:
        from wbi import get_signer
        return get_signer().sign(params)
    except Exception:
        # 获取签名 key 失败时退回未签名请求（该接口不强制要求签名）
        return params


def fetch_comments(
    oid: int,
    comment_type: int = 1,
    max_pages: int = 100,
    sort_mode: int = 2,
    progress_callback=None,
    limiter=None,
    raise_on_incomplete: bool = False,
) -> tuple[list[Comment], int]:
    """采集视频评论

    使用浏览器 headers + Cookie 会话 + WBI 签名，
    减少 412 限流概率。

    Args:
        oid: 视频 aid
        comment_type: 评论区类型 (1=视频)
        max_pages: 最大页数（默认 100）
        sort_mode: 排序模式 (2=时间倒序, 3=热度排序)
        progress_callback: 进度回调 (collected_count, total_count)
        limiter: 可选的 AdaptiveRateLimiter，提供时由它控制页间延迟
        raise_on_incomplete: 限流重试耗尽或中途出错时抛出 CommentsIncomplete，
            而不是返回已采到的部分（全量采集需要区分"采完"和"没采完"）

    Returns:
        (评论列表, 总评论数)
    """
    url = "https://api.bilibili.com/x/v2/reply/main"
    client = _build_session()

    comments: list[Comment] = []
    seen_rpids: set[int] = set()
    page = 0
    total_count = 0
    ban_retries = 0
    max_ban_retries = 8
    network_retries = 0
    consecutive_success = 0
    page_delay = 3.0
    cursor_next = 0  # cursor 分页游标，0 表示第一页

    def rebuild_session():
        nonlocal client
        client.close()
        client = _build_session()

    def stop_incomplete(reason: str):
        if raise_on_incomplete:
            raise CommentsIncomplete(reason, len(comments))

    try:
        while page < max_pages:
            params = {
                "oid": oid,
                "type": comment_type,
                "mode": sort_mode,
                "ps": 20,
                "pn": 1,
            }
            # cursor 分页：B站用 'next' 参数传 cursor 值
            if cursor_next > 0:
                params["next"] = cursor_next

            # 网络层错误（超时、SSL 断连、连接重置）：等待后重试同一页，保留已采到的进度
            try:
                resp = client.get(url, params=_try_sign_params(params))
            except httpx.TransportError as e:
                network_retries += 1
                if network_retries > MAX_NETWORK_RETRIES:
                    stop_incomplete(f"网络连续出错 {MAX_NETWORK_RETRIES} 次仍未恢复: {e}")
                    raise
                wait = min(15 * 2 ** (network_retries - 1), 240)
                print(f"  网络出错（{type(e).__name__}），{wait}s 后重试第 {network_retries} 次"
                      f"（已采 {len(comments)} 条）...", flush=True)
                time.sleep(wait)
                rebuild_session()
                continue
            network_retries = 0

            # 412 反爬 → 指数退避
            if resp.status_code == 412:
                ban_retries += 1
                consecutive_success = 0
                if limiter:
                    limiter.record_failure()
                if ban_retries > max_ban_retries:
                    if len(comments) == 0:
                        print(f"  评论被限流(412)，已重试{ban_retries}次仍失败，跳过")
                    stop_incomplete(f"评论被限流(412)，重试 {max_ban_retries} 次仍失败")
                    break
                wait = min(ban_retries * 30 + 10, 180)
                if len(comments) == 0:
                    print(f"  评论限流(412)，等待 {wait}s (第{ban_retries}次)...")
                time.sleep(wait)
                rebuild_session()
                continue

            # 403 拦截（原先还按响应头里是否含 "cf-" 判断，容易误判，已去掉）
            if resp.status_code == 403:
                ban_retries += 1
                consecutive_success = 0
                if limiter:
                    limiter.record_failure()
                if ban_retries > max_ban_retries:
                    stop_incomplete(f"评论被拦截(403)，重试 {max_ban_retries} 次仍失败")
                    break
                wait = ban_retries * 20
                print(f"  评论被拦截(403)，等待 {wait}s...")
                time.sleep(wait)
                rebuild_session()
                continue
            ban_retries = 0

            try:
                data = resp.json()
            except Exception:
                if len(comments) == 0:
                    print(f"  (评论区不可用: 非 JSON 响应)")
                else:
                    stop_incomplete(f"第 {page + 1} 页返回非 JSON 响应")
                break

            if data["code"] != 0:
                # 12002=评论区关闭, -404=无此资源，其余错误仅首页时提示
                if len(comments) == 0 and data["code"] not in (12002, -404):
                    print(f"  评论接口报错 code={data['code']}: {data.get('message', '')}")
                if len(comments) > 0 or data["code"] not in (12002, -404):
                    stop_incomplete(f"评论接口报错 code={data['code']}: {data.get('message', '')}")
                break

            result = data["data"]
            if total_count == 0:
                total_count = (result.get("page", {}).get("count", 0) or
                              result.get("cursor", {}).get("all_count", 0))

            replies_list = result.get("replies") or []
            if not replies_list:
                break

            new_count = 0
            for item in replies_list:
                if item["rpid"] in seen_rpids:
                    continue
                seen_rpids.add(item["rpid"])
                new_count += 1
                sub_replies = []
                for sub in item.get("replies") or []:
                    sub_replies.append(CommentReply(
                        rpid=sub["rpid"],
                        mid=sub["mid"],
                        member_name=sub["member"]["uname"],
                        content=sub["content"]["message"],
                        ctime=sub["ctime"],
                        like=sub["like"],
                        parent_rpid=item["rpid"],
                    ))
                comments.append(Comment(
                    rpid=item["rpid"],
                    oid=item["oid"],
                    mid=item["mid"],
                    member_name=item["member"]["uname"],
                    content=item["content"]["message"],
                    ctime=item["ctime"],
                    like=item["like"],
                    rcount=item["rcount"],
                    replies=sub_replies,
                    up_replied=bool((item.get("up_action") or {}).get("reply")),
                ))

            # 整页都是已采集过的评论：接口没有按游标翻页，继续请求只会得到重复数据
            if new_count == 0:
                break

            page += 1
            consecutive_success += 1
            if limiter:
                limiter.record_success()

            if progress_callback:
                progress_callback(len(comments), total_count)

            # 检查 cursor 是否到达末尾
            cursor_info = result.get("cursor", {})
            if cursor_info.get("is_end") and cursor_info.get("is_begin"):
                # 特殊：有些模式只有第一页
                if len(replies_list) < 20:
                    break

            # 获取下一页 cursor
            new_cursor = cursor_info.get("next", 0)
            if new_cursor == 0 or new_cursor == cursor_next:
                break
            cursor_next = new_cursor

            # 页间延迟：有限流器时交给它，否则使用内置的简单衰减
            if limiter:
                limiter.wait()
            else:
                if consecutive_success > 5:
                    page_delay = max(2.0, page_delay * 0.9)
                time.sleep(page_delay)
    finally:
        client.close()

    return comments, total_count


def fetch_comments_full(
    oid: int,
    comment_type: int = 1,
    progress_callback=None,
    limiter=None,
) -> tuple[list[Comment], int, dict]:
    """全量采集一级评论：按时间排序一直翻到最后一页

    时间排序能遍历全部一级评论，全量时热度排序没有额外收益，因此只用一种排序。
    没有采完（限流重试耗尽、中途出错）时抛出 CommentsIncomplete。

    Returns:
        (评论列表, 接口给出的评论总数（含楼中楼）, 采集信息)
    """
    comments, total = fetch_comments(
        oid, comment_type=comment_type, max_pages=FULL_MAX_PAGES, sort_mode=2,
        progress_callback=progress_callback, limiter=limiter, raise_on_incomplete=True,
    )
    info = {
        "strategy": "full",
        "collected": len(comments),
        "reported_total": total,
        "sub_replies": sum(c.rcount for c in comments),
    }
    return comments, total, info


def fetch_comment_replies(
    oid: int,
    root_rpid: int,
    comment_type: int = 1,
    max_pages: int = 10,
) -> list[CommentReply]:
    """采集某条主评论下的全部子回复"""
    replies: list[CommentReply] = []
    page = 0

    with make_client(BROWSER_HEADERS) as client:
        while page < max_pages:
            resp = client.get(
                "https://api.bilibili.com/x/v2/reply/reply",
                params={
                    "oid": oid,
                    "type": comment_type,
                    "root": root_rpid,
                    "pn": page + 1,
                    "ps": 20,
                },
            )
            data = resp.json()
            if data["code"] != 0:
                break

            items = data["data"].get("replies") or []
            for item in items:
                replies.append(CommentReply(
                    rpid=item["rpid"],
                    mid=item["mid"],
                    member_name=item["member"]["uname"],
                    content=item["content"]["message"],
                    ctime=item["ctime"],
                    like=item["like"],
                    parent_rpid=root_rpid,
                ))

            if len(items) < 20:
                break
            page += 1
            time.sleep(0.6)

    return replies


# ── 评论最大化采集 ────────────────────────────────────────

def _dedup_comments(list_a: list[Comment], list_b: list[Comment]) -> list[Comment]:
    """按 rpid 去重合并两个评论列表

    策略：保留 rcount 更大、sub-replies 更多的版本
    """
    merged: dict[int, Comment] = {}

    for c in (*list_a, *list_b):
        existing = merged.get(c.rpid)
        if existing is None:
            merged[c.rpid] = c
            continue
        up_replied = existing.up_replied or c.up_replied
        # 保留 rcount 更大、回复更多的版本
        if c.rcount > existing.rcount or len(c.replies) > len(existing.replies):
            merged[c.rpid] = c
        merged[c.rpid].up_replied = up_replied

    # 按 ctime 降序排列（最新在前）
    result = sorted(merged.values(), key=lambda x: x.ctime, reverse=True)
    return result


def fetch_comments_maximized(
    oid: int,
    comment_type: int = 1,
    max_pages_per_mode: int = 100,
    progress_callback=None,
    limiter=None,
) -> tuple[list[Comment], int, dict]:
    """最大化评论采集：双模式 + 去重

    先以 mode=2（时间倒序）采集，再以 mode=3（热度排序）采集。
    两种排序返回不同的评论子集，合并后通过 rpid 去重。

    Args:
        oid: 视频 aid
        comment_type: 评论区类型 (1=视频)
        max_pages_per_mode: 每种模式的最大页数
        progress_callback: 进度回调 (collected, total, phase)
        limiter: 可选的 AdaptiveRateLimiter，控制页间延迟

    Returns:
        (合并去重后的评论列表, 去重后总数, 统计信息字典)
    """
    stats = {
        "mode2_count": 0,
        "mode3_count": 0,
        "overlap_count": 0,
        "deduped_total": 0,
        "mode2_exhausted": False,
        "mode3_exhausted": False,
    }

    # Phase 1: mode=2 时间倒序（最新评论）
    if progress_callback:
        progress_callback(0, 0, "mode2")
    comments_mode2, total2 = fetch_comments(
        oid, comment_type=comment_type,
        max_pages=max_pages_per_mode, sort_mode=2, limiter=limiter,
    )
    stats["mode2_count"] = len(comments_mode2)
    stats["mode2_exhausted"] = len(comments_mode2) < max_pages_per_mode * 20

    # Phase 2: mode=3 热度排序（最热评论）
    if progress_callback:
        progress_callback(0, 0, "mode3")
    comments_mode3, total3 = fetch_comments(
        oid, comment_type=comment_type,
        max_pages=max_pages_per_mode, sort_mode=3, limiter=limiter,
    )
    stats["mode3_count"] = len(comments_mode3)
    stats["mode3_exhausted"] = len(comments_mode3) < max_pages_per_mode * 20

    # 去重合并
    merged = _dedup_comments(comments_mode2, comments_mode3)
    stats["deduped_total"] = len(merged)
    # 计算真正重叠：两个列表中都出现的 rpid 数
    rpids_mode2 = {c.rpid for c in comments_mode2}
    rpids_mode3 = {c.rpid for c in comments_mode3}
    stats["overlap_count"] = len(rpids_mode2 & rpids_mode3)
    stats["mode2_only"] = len(rpids_mode2 - rpids_mode3)
    stats["mode3_new"] = len(rpids_mode3 - rpids_mode2)

    # 使用更大的 total 值
    final_total = max(total2, total3, stats["deduped_total"])

    if progress_callback:
        progress_callback(len(merged), final_total, "done")

    return merged, final_total, stats
