"""
B站周热榜采集模块
获取每周必看视频列表
"""
import httpx
from dataclasses import dataclass, asdict
from typing import Optional

try:
    from .wbi import get_signer
except ImportError:
    from wbi import get_signer

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com",
}


@dataclass
class VideoInfo:
    """视频基本信息"""
    aid: int
    bvid: str
    cid: int
    title: str
    owner_name: str
    owner_mid: int
    view: int
    danmaku: int
    reply: int
    favorite: int
    coin: int
    share: int
    like: int
    duration: int  # 秒
    width: int     # 视频宽度
    height: int    # 视频高度
    pic: str
    pubdate: int  # 发布时间戳


def fetch_weekly_series() -> list[dict]:
    """获取所有每周必看期号列表"""
    client = httpx.Client(headers=BASE_HEADERS, timeout=15, trust_env=False)
    resp = client.get("https://api.bilibili.com/x/web-interface/popular/series/list")
    data = resp.json()
    if data["code"] != 0:
        raise Exception(f"获取期号列表失败: {data}")
    return data["data"]["list"]  # 包含 number, name, subject 等


def fetch_weekly_videos(series_number: int = None, raw: bool = False) -> list:
    """获取指定期号的每周必看视频列表

    Args:
        series_number: 期号，为 None 时获取最新一期
        raw: True 返回原始 JSON，False 返回 VideoInfo 列表
    """
    import time as _time

    params = {}
    if series_number is not None:
        params["number"] = series_number

    # 带重试的请求
    max_retries = 3
    for attempt in range(max_retries):
        signer = get_signer()
        resp = signer.signed_get(
            "https://api.bilibili.com/x/web-interface/popular/series/one",
            params=params,
        )
        data = resp.json()
        if data["code"] == 0:
            break
        elif data["code"] == -352:
            # Wbi 签名失败，重新获取 key 后重试
            signer._last_refresh = 0  # 强制刷新
            _time.sleep(10 + attempt * 5)
        else:
            raise Exception(f"获取视频列表失败: {data}")
    else:
        raise Exception(f"获取视频列表失败 (已重试{max_retries}次): {data}")

    result = data["data"]
    items = result.get("list", [])

    if raw:
        return items

    videos = []
    for item in items:
        stat = item.get("stat", {})
        owner = item.get("owner", {})
        videos.append(VideoInfo(
            aid=item["aid"],
            bvid=item["bvid"],
            cid=item.get("cid", 0),
            title=item["title"],
            owner_name=owner.get("name", ""),
            owner_mid=owner.get("mid", 0),
            view=stat.get("view", 0),
            danmaku=stat.get("danmaku", 0),
            reply=stat.get("reply", 0),
            favorite=stat.get("favorite", 0),
            coin=stat.get("coin", 0),
            share=stat.get("share", 0),
            like=stat.get("like", 0),
            duration=item.get("duration", 0),
            width=0,
            height=0,
            pic=item.get("pic", ""),
            pubdate=item.get("pubdate", 0),
        ))

    return videos


def get_series_info(series_number: int) -> dict:
    """获取某期的元信息（标题、封面等）"""
    import time as _time

    for attempt in range(3):
        signer = get_signer()
        resp = signer.signed_get(
            "https://api.bilibili.com/x/web-interface/popular/series/one",
            params={"number": series_number},
        )
        data = resp.json()
        if data["code"] == 0:
            break
        elif data["code"] == -352:
            signer._last_refresh = 0
            _time.sleep(10 + attempt * 5)
        else:
            print(f"  获取期号信息失败: code={data.get('code')} msg={data.get('message')}")
            return {}
    else:
        print(f"  获取期号信息失败: 重试3次后仍然失败")
        return {}
    meta = data["data"]
    config = meta.get("config", {})
    return {
        "number": config.get("number", meta.get("number", series_number)),
        "name": config.get("name", meta.get("name", "")),
        "subject": config.get("subject", meta.get("subject", "")),
        "banner_url": meta.get("banner_url", ""),
        "video_count": len(meta.get("list", [])),
    }
