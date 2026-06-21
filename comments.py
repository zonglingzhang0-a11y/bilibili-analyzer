"""
B站评论采集模块
使用 pn/ps 分页，20条/页
使用完整浏览器 headers + Cookie 会话避免 412 封禁
"""
import os
import time
import httpx
from dataclasses import dataclass, field, asdict

# 完整浏览器 headers，模拟正常访问
BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin": "https://www.bilibili.com",
    "Referer": "https://www.bilibili.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "Connection": "keep-alive",
}


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


def _build_session() -> httpx.Client:
    """创建带 Cookie 的 httpx 会话，模拟正常浏览器访问"""
    client = httpx.Client(headers=BASE_HEADERS, timeout=20, trust_env=False,
                          follow_redirects=True)

    # 先访问 B站首页获取初始 Cookie
    try:
        resp = client.get("https://www.bilibili.com/")
    except Exception:
        pass

    # 尝试读取保存的登录 Cookie（.sessdata 文件）
    sessdata_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".sessdata"
    )
    if os.path.isfile(sessdata_path):
        try:
            with open(sessdata_path, "r", encoding="utf-8") as f:
                sessdata = f.read().strip()
            if sessdata:
                client.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")
        except Exception:
            pass

    # 确保有 buvid3（B站设备标识）
    if "buvid3" not in client.cookies:
        client.cookies.set("buvid3", "auto", domain=".bilibili.com")

    return client


def _try_sign_params(params: dict) -> dict:
    """尝试对评论请求参数进行 WBI 签名"""
    try:
        from wbi import get_signer
        signer = get_signer()
        return signer.signed_get_params(params)
    except Exception:
        return params


def fetch_comments(
    oid: int,
    comment_type: int = 1,
    max_pages: int = 100,
    sort_mode: int = 2,
    progress_callback=None,
) -> tuple[list[Comment], int]:
    """采集视频评论

    使用浏览器 headers + Cookie 会话 + WBI 签名尝试，
    减少 412 限流概率。

    Args:
        oid: 视频 aid
        comment_type: 评论区类型 (1=视频)
        max_pages: 最大页数（默认 100）
        sort_mode: 排序模式 (2=时间倒序, 3=热度排序)
        progress_callback: 进度回调 (collected_count, total_count)

    Returns:
        (评论列表, 总评论数)
    """
    client = _build_session()
    url = "https://api.bilibili.com/x/v2/reply/main"

    comments: list[Comment] = []
    page = 0
    total_count = 0
    ban_retries = 0
    max_ban_retries = 8
    consecutive_success = 0
    page_delay = 3.0
    cursor_next = 0  # cursor 分页游标，0 表示第一页

    while page < max_pages:
        params = {
            "oid": oid,
            "type": comment_type,
            "mode": sort_mode,
            "ps": 20,
        }
        # cursor 分页：B站用 'next' 参数传 cursor 值
        if cursor_next > 0:
            params["pn"] = 1
            params["next"] = cursor_next
        else:
            params["pn"] = 1

        signed_params = _try_sign_params(params)

        resp = client.get(url, params=signed_params)

        # 412 反爬 → 指数退避
        if resp.status_code == 412:
            ban_retries += 1
            consecutive_success = 0
            if ban_retries > max_ban_retries:
                if len(comments) == 0:
                    print(f"  评论被限流(412)，已重试{ban_retries}次仍失败，跳过")
                break
            wait = min(ban_retries * 30 + 10, 180)
            if len(comments) == 0:
                print(f"  评论限流(412)，等待 {wait}s (第{ban_retries}次)...")
            time.sleep(wait)
            client = _build_session()
            continue
        ban_retries = 0

        # 检查 Cloudflare/反爬
        if resp.status_code == 403 or "cf-" in str(resp.headers).lower():
            ban_retries += 1
            consecutive_success = 0
            if ban_retries > max_ban_retries:
                break
            wait = ban_retries * 20
            print(f"  评论被拦截(403)，等待 {wait}s...")
            time.sleep(wait)
            client = _build_session()
            continue

        try:
            data = resp.json()
        except Exception:
            if len(comments) == 0:
                print(f"  (评论区不可用: 非 JSON 响应)")
            break

        if data["code"] != 0:
            if data["code"] == 12002:
                break
            if data["code"] == -404:
                break
            if len(comments) == 0:
                print(f"  评论接口报错 code={data['code']}: {data.get('message', '')}")
                if data["code"] in (-352, -400):
                    break
            break

        result = data["data"]
        if total_count == 0:
            total_count = (result.get("page", {}).get("count", 0) or
                          result.get("cursor", {}).get("all_count", 0))

        replies_list = result.get("replies") or []
        if not replies_list:
            break

        for item in replies_list:
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
            ))

        page += 1
        consecutive_success += 1

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

        # 自适应页间延迟
        if consecutive_success > 5:
            page_delay = max(2.0, page_delay * 0.9)
        time.sleep(page_delay)

    return comments, total_count


def fetch_comment_replies(
    oid: int,
    root_rpid: int,
    comment_type: int = 1,
    max_pages: int = 10,
) -> list[CommentReply]:
    """采集某条主评论下的全部子回复"""
    client = httpx.Client(headers=BASE_HEADERS, timeout=15, trust_env=False)
    replies: list[CommentReply] = []
    page = 0

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

    for c in list_a:
        if c.rpid in merged:
            existing = merged[c.rpid]
            # 保留 rcount 更大、回复更多的版本
            if (c.rcount > existing.rcount or
                len(c.replies) > len(existing.replies)):
                merged[c.rpid] = c
        else:
            merged[c.rpid] = c

    for c in list_b:
        if c.rpid in merged:
            existing = merged[c.rpid]
            if (c.rcount > existing.rcount or
                len(c.replies) > len(existing.replies)):
                merged[c.rpid] = c
        else:
            merged[c.rpid] = c

    # 按 ctime 降序排列（最新在前）
    result = sorted(merged.values(), key=lambda x: x.ctime, reverse=True)
    return result


def fetch_comments_maximized(
    oid: int,
    comment_type: int = 1,
    max_pages_per_mode: int = 100,
    progress_callback=None,
) -> tuple[list[Comment], int, dict]:
    """最大化评论采集：双模式 + 去重

    先以 mode=2（时间倒序）采集，再以 mode=3（热度排序）采集。
    两种排序返回不同的评论子集，合并后通过 rpid 去重。

    Args:
        oid: 视频 aid
        comment_type: 评论区类型 (1=视频)
        max_pages_per_mode: 每种模式的最大页数
        progress_callback: 进度回调 (collected, total, phase)

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
        max_pages=max_pages_per_mode, sort_mode=2,
    )
    stats["mode2_count"] = len(comments_mode2)
    stats["mode2_exhausted"] = len(comments_mode2) < max_pages_per_mode * 20

    # Phase 2: mode=3 热度排序（最热评论）
    if progress_callback:
        progress_callback(0, 0, "mode3")
    comments_mode3, total3 = fetch_comments(
        oid, comment_type=comment_type,
        max_pages=max_pages_per_mode, sort_mode=3,
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
