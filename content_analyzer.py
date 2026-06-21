"""
视频内容理解模块
硬参数采集 + 封面分析 + 弹幕高潮帧分析
零完整视频下载，仅封面图 + 少数关键帧截图
"""
import os
import struct
import json
import time
from datetime import datetime
import httpx
from dataclasses import dataclass, asdict
from io import BytesIO

from ranking import VideoInfo

BASE_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com",
}

try:
    from .wbi import get_signer
except ImportError:
    from wbi import get_signer

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False


def fetch_hardware_params(aid: int) -> dict:
    """从 view API 获取视频硬参数"""
    signer = get_signer()
    resp = signer.signed_get(
        "https://api.bilibili.com/x/web-interface/view",
        params={"aid": aid},
    )
    if resp.json().get("code") != 0:
        return {}

    d = resp.json()["data"]
    dim = d.get("dimension", {})
    pages = d.get("pages", [])

    result = {
        "bvid": d.get("bvid", ""),
        "width": dim.get("width", 0),
        "height": dim.get("height", 0),
        "duration_seconds": d.get("duration", 0),
        "pages_count": len(pages),
        "pic_url": d.get("pic", ""),
        "desc": d.get("desc", "")[:200],  # 简介截断
    }

    # 分P详情
    if pages:
        page_info = []
        for p in pages:
            pdim = p.get("dimension", {})
            page_info.append({
                "cid": p["cid"],
                "part": p.get("part", ""),
                "duration": p.get("duration", 0),
                "width": pdim.get("width", 0),
                "height": pdim.get("height", 0),
                "first_frame": p.get("first_frame", ""),
            })
        result["pages"] = page_info

    return result


def download_cover(pic_url: str, output_path: str) -> str | None:
    """下载封面图

    Args:
        pic_url: 封面图 URL
        output_path: 输出文件路径（含文件名）

    Returns:
        成功返回文件路径，失败返回 None
    """
    if not pic_url:
        return None

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    try:
        client = httpx.Client(headers=BASE_HEADERS, timeout=30, trust_env=False)
        resp = client.get(pic_url)
        if resp.status_code == 200 and len(resp.content) > 100:
            with open(output_path, "wb") as f:
                f.write(resp.content)
            return output_path
    except Exception as e:
        print(f"    封面下载失败: {e}")
    return None


def _get_videoshot_info(aid: int, cid: int) -> dict | None:
    """获取 B站 视频缩略图精灵图信息

    Returns:
        {
            "images": ["https://...", ...],  # 精灵图 URL 列表
            "x_len": 10, "y_len": 10,         # 每张精灵图的缩略图网格
            "x_size": 480, "y_size": 270,      # 每个缩略图的像素尺寸
            "timestamps": [0, 8000, 16000, ...],  # 每个位置的毫秒时间戳 (如果解析成功)
            "total_shots": 175,                # 总缩略图数
        }
    """
    signer = get_signer()
    resp = signer.signed_get(
        "https://api.bilibili.com/x/player/videoshot",
        params={"aid": aid, "cid": cid},
    )
    if resp.json().get("code") != 0:
        return None

    d = resp.json()["data"]
    images = ["https:" + url for url in d.get("image", [])]
    if not images:
        return None

    info = {
        "images": images,
        "x_len": d.get("img_x_len", 10),
        "y_len": d.get("img_y_len", 10),
        "x_size": d.get("img_x_size", 480),
        "y_size": d.get("img_y_size", 270),
        "timestamps": [],
        "total_shots": len(images) * d.get("img_x_len", 10) * d.get("img_y_len", 10),
    }

    # 尝试解析 pvdata 二进制索引获取精确时间戳
    pvdata = d.get("pvdata", "")
    if pvdata:
        pvdata_url = "https:" + pvdata
        try:
            client = httpx.Client(headers=BASE_HEADERS, timeout=15, trust_env=False)
            bin_resp = client.get(pvdata_url)
            if bin_resp.status_code == 200 and len(bin_resp.content) >= 4:
                raw = bin_resp.content
                # pvdata 格式: 前 4 字节为头部，之后为 uint16 BE 数组（shot index × 8）
                count = (len(raw) - 4) // 2
                if count > 0:
                    stamps = struct.unpack(f">{count}H", raw[4:4 + count * 2])
                    # B站按 8 帧递增存入，转为实际帧号后再乘帧间隔估算时间
                    # shot_index × 8 = 帧偏移，约 30fps → 每帧 ~33ms
                    # 简化：直接存 shot_index（×8 帧号），供后续查找
                    info["timestamps"] = list(stamps)
                    info["total_shots"] = len(stamps)
        except Exception:
            pass

    return info


def capture_frames(
    bvid: str,
    timestamps: list[dict],
    output_dir: str,
    cid: int = 0,
    aid: int = 0,
    duration_seconds: int = 0,
) -> list[dict]:
    """用 B站 videoshot 精灵图 API 截取关键帧缩略图

    Args:
        bvid: 视频 BV 号（保留兼容，实际用 aid/cid）
        timestamps: [{"time": "17:12", "density": 13, ...}, ...]
        output_dir: 帧截图输出目录
        cid: 分P cid
        aid: 视频 aid
        duration_seconds: 视频时长（秒），用于估算缩略图时间位置

    Returns:
        [{"timestamp_info": ..., "frame_path": "xxx.jpg"}, ...]
    """
    if not timestamps:
        return []

    os.makedirs(output_dir, exist_ok=True)

    info = _get_videoshot_info(aid, cid)
    if not info:
        print("    无法获取视频缩略图信息")
        return []

    total_shots = info["total_shots"]
    x_len = info["x_len"]
    y_len = info["y_len"]
    x_size = info["x_size"]
    y_size = info["y_size"]
    images = info["images"]
    shots_per_sheet = x_len * y_len
    exact_timestamps = info.get("timestamps", [])

    client = httpx.Client(headers=BASE_HEADERS, timeout=60, trust_env=False)
    results = []

    # 如果已知精确毫秒时间戳，从 pvdata 映射；否则按等距估算
    if exact_timestamps and duration_seconds > 0:
        # pvdata 存储的是 shot_index（×8 帧号），等距映射到时间轴
        all_stamps_ms = []
        for si in exact_timestamps:
            # shot_index 映射到毫秒（假设均匀分布）
            frac = si / exact_timestamps[-1] if exact_timestamps[-1] > 0 else 0
            all_stamps_ms.append(int(frac * duration_seconds * 1000))
    else:
        # 无精确数据，等距分布
        all_stamps_ms = [
            int((i / max(total_shots - 1, 1)) * duration_seconds * 1000)
            for i in range(total_shots)
        ] if duration_seconds > 0 else []

    for ts_info in timestamps:
        time_str = ts_info["time"]  # "MM:SS" 格式
        safe_time = time_str.replace(":", "m") + "s"
        frame_path = os.path.join(output_dir, f"frame_{safe_time}.jpg")

        # 如果已经截过就跳过
        if os.path.exists(frame_path) and os.path.getsize(frame_path) > 0:
            results.append({"timestamp_info": ts_info, "frame_path": frame_path})
            continue

        try:
            # 转换目标时间 → 毫秒
            parts = time_str.split(":")
            target_ms = (int(parts[0]) * 60 + int(parts[1])) * 1000

            # 查找最接近的缩略图索引
            shot_idx = 0
            if all_stamps_ms:
                shot_idx = min(range(len(all_stamps_ms)),
                              key=lambda i: abs(all_stamps_ms[i] - target_ms))

            # 确定在哪个精灵图的哪个位置
            sheet_idx = shot_idx // shots_per_sheet
            pos_in_sheet = shot_idx % shots_per_sheet
            col = pos_in_sheet % x_len
            row = pos_in_sheet // x_len

            if sheet_idx >= len(images):
                continue

            # 下载精灵图
            sprite_url = images[sheet_idx]
            img_resp = client.get(sprite_url)
            if img_resp.status_code != 200:
                continue

            # 裁剪缩略图
            if HAS_PIL:
                img = Image.open(BytesIO(img_resp.content))
                left = col * x_size
                top = row * y_size
                right = left + x_size
                bottom = top + y_size
                cropped = img.crop((left, top, right, bottom))
                cropped.save(frame_path, "JPEG", quality=85)
            else:
                # 无 PIL 时直接保存原精灵图
                with open(frame_path, "wb") as f:
                    f.write(img_resp.content)

            if os.path.exists(frame_path) and os.path.getsize(frame_path) > 0:
                results.append({"timestamp_info": ts_info, "frame_path": frame_path})
            else:
                print(f"    截帧失败 {time_str}: 文件为空")

        except Exception as e:
            print(f"    截帧异常 {time_str}: {e}")

    return results


def build_content_prompts(
    cover_path: str | None,
    frame_results: list[dict],
    video_title: str,
) -> dict:
    """生成用于 MCP 分析的结构化提示

    Returns:
        {
            "cover_prompt": str,
            "cover_abs_path": str | None,
            "frame_prompts": [{"time": ..., "prompt": str,
                               "image_abs_path": str}, ...]
        }
    """
    prompts = {}

    if cover_path:
        prompts["cover_prompt"] = (
            f"分析这个B站视频的封面图。视频标题：{video_title}。"
            f"请识别：1.封面画面的主题和核心视觉元素 "
            f"2.画面风格（写实/手绘/纯文字/影视截图/表情包等）"
            f"3.画面传达的情绪基调（严肃/搞笑/温馨/燃/怀旧等）"
            f"4.封面上可见的文字内容"
            f"5.封面属于什么内容类型（影视剪辑/游戏/知识科普/生活/vlog等）"
        )
        prompts["cover_abs_path"] = os.path.abspath(cover_path)

    if frame_results:
        frame_prompts = []
        for fr in frame_results:
            ti = fr["timestamp_info"]
            samples = " | ".join(ti.get("sample", [])[:4])
            frame_entry = {
                "time": ti["time"],
                "density": ti.get("density", 0),
                "prompt": (
                    f"分析这个B站视频的截图。视频标题：{video_title}。"
                    f"截图时间点：{ti['time']}。"
                    f"此时弹幕密度为 {ti.get('density', 0)} 条/6秒（弹幕高潮）。"
                    f"这个时间点的弹幕样本：{samples}。"
                    f"请分析：1.画面中发生了什么场景/事件？"
                    f"2.画面的情绪/氛围是什么？"
                    f"3.为什么这个时刻会引爆弹幕？画面和弹幕之间的关系是什么？"
                ),
                "image_abs_path": os.path.abspath(fr["frame_path"]),
            }
            frame_prompts.append(frame_entry)
        prompts["frame_prompts"] = frame_prompts

    return prompts


def generate_mcp_task_list(output_base: str) -> str | None:
    """扫描输出目录，为所有视频生成统一 MCP 分析任务清单

    Returns:
        mcp_manifest.json 的路径，如果没有任务则返回 None
    """
    import json

    tasks = []
    for entry_name in sorted(os.listdir(output_base)):
        video_dir = os.path.join(output_base, entry_name)
        if not os.path.isdir(video_dir):
            continue

        # 解析 aid 和标题
        parts = entry_name.split("_", 1)
        try:
            aid = int(parts[0])
        except ValueError:
            continue
        title = parts[1] if len(parts) > 1 else ""

        prompts_file = os.path.join(video_dir, "content_prompts.json")
        if not os.path.isfile(prompts_file):
            continue

        try:
            with open(prompts_file, "r", encoding="utf-8") as f:
                prompts = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        # 封面任务
        if prompts.get("cover_abs_path") and os.path.isfile(prompts["cover_abs_path"]):
            result_path = os.path.join(video_dir, "cover_analysis.json")
            tasks.append({
                "task_id": f"cover_{aid}",
                "type": "cover",
                "aid": aid,
                "title": title,
                "video_dir": entry_name,
                "image_path": prompts["cover_abs_path"],
                "prompt": prompts.get("cover_prompt", ""),
                "status": "completed" if os.path.isfile(result_path) else "pending",
                "result_path": result_path,
            })

        # 帧任务
        for fi, fp in enumerate(prompts.get("frame_prompts", [])):
            if not fp.get("image_abs_path") or not os.path.isfile(fp["image_abs_path"]):
                continue
            time_str = fp.get("time", f"frame{fi}").replace(":", "m").replace(":", "s")
            result_path = os.path.join(video_dir, f"frame_{time_str}_analysis.json")
            tasks.append({
                "task_id": f"frame_{fi}_{aid}",
                "type": "frame",
                "aid": aid,
                "title": title,
                "video_dir": entry_name,
                "time": fp.get("time", ""),
                "density": fp.get("density", 0),
                "image_path": fp["image_abs_path"],
                "prompt": fp.get("prompt", ""),
                "status": "completed" if os.path.isfile(result_path) else "pending",
                "result_path": result_path,
            })

    if not tasks:
        return None

    manifest = {
        "generated_at": datetime.now().isoformat(),
        "output_base": os.path.abspath(output_base),
        "total_tasks": len(tasks),
        "pending_tasks": sum(1 for t in tasks if t["status"] == "pending"),
        "completed_tasks": sum(1 for t in tasks if t["status"] == "completed"),
        "tasks": tasks,
    }

    manifest_path = os.path.join(output_base, "mcp_manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    return manifest_path
