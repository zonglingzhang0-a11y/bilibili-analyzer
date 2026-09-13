"""
B站周热榜采集模块
获取每周必看视频列表
"""
import time
from dataclasses import dataclass
from functools import lru_cache

from bili_http import make_client
from wbi import get_signer


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
    with make_client() as client:
        resp = client.get("https://api.bilibili.com/x/web-interface/popular/series/list")
    data = resp.json()
    if data["code"] != 0:
        raise Exception(f"获取期号列表失败: {data}")
    return data["data"]["list"]  # 包含 number, name, subject 等


def get_latest_series_number() -> int:
    """最新一期的期号"""
    return max(item["number"] for item in fetch_weekly_series())


@lru_cache(maxsize=8)
def _fetch_series_one(series_number: int = None, max_retries: int = 3) -> dict:
    """请求 popular/series/one，遇到 -352（签名失效）时刷新 key 后重试

    series_number 为 None 时先从期号列表取最新期号：该接口不带 number 参数时
    返回的是第 1 期（2019 年），而不是最新一期。

    结果按期号缓存：主流程会先后调用 get_series_info 和 fetch_weekly_videos，
    避免同一期重复请求。返回值请勿修改。

    Returns:
        接口返回的 data 字段
    """
    if series_number is None:
        series_number = get_latest_series_number()
    params = {"number": series_number}

    signer = get_signer()
    data = {}
    for attempt in range(max_retries):
        resp = signer.signed_get(
            "https://api.bilibili.com/x/web-interface/popular/series/one",
            params=params,
        )
        data = resp.json()
        if data["code"] == 0:
            return data["data"]
        if data["code"] != -352:
            raise Exception(f"获取每周必看失败: code={data.get('code')} msg={data.get('message')}")
        # Wbi 签名失败，丢弃 key 后重试
        signer.invalidate()
        time.sleep(10 + attempt * 5)
    raise Exception(f"获取每周必看失败 (已重试{max_retries}次): {data}")


def fetch_weekly_videos(series_number: int = None, raw: bool = False) -> list:
    """获取指定期号的每周必看视频列表

    Args:
        series_number: 期号，为 None 时获取最新一期
        raw: True 返回原始 JSON，False 返回 VideoInfo 列表
    """
    result = _fetch_series_one(series_number)
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
    try:
        meta = _fetch_series_one(series_number)
    except Exception as e:
        print(f"  获取期号信息失败: {e}")
        return {}
    config = meta.get("config", {})
    return {
        "number": config.get("number", meta.get("number", series_number)),
        "name": config.get("name", meta.get("name", "")),
        "subject": config.get("subject", meta.get("subject", "")),
        "banner_url": meta.get("banner_url", ""),
        "video_count": len(meta.get("list", [])),
    }
