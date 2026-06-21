"""
B站弹幕采集模块
分段获取 + Protobuf 解码
"""
import gzip
import httpx
import time
from dataclasses import dataclass


@dataclass
class Danmaku:
    """弹幕"""
    id: int
    progress: int       # 视频时间(ms)
    mode: int           # 1=滚动, 4=底部, 5=顶部, 6=逆向, 7=高级
    fontsize: int       # 字号
    color: int          # 颜色(十进制)
    mid_hash: str       # 用户哈希
    content: str        # 弹幕文本
    ctime: int          # 发送时间戳
    weight: int         # 权重
    pool: int           # 0=普通, 1=字幕, 2=特殊


class ProtobufReader:
    """简易 Protobuf wire-format 解析器"""

    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def _read_varint(self) -> int:
        """读取 varint"""
        result = 0
        shift = 0
        while self.pos < len(self.data):
            b = self.data[self.pos]
            self.pos += 1
            result |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        return result

    def _read_tag(self) -> tuple[int, int]:
        """读取 tag，返回 (field_number, wire_type)"""
        v = self._read_varint()
        return v >> 3, v & 0x07

    def _read_length_delimited(self) -> bytes:
        """读取 length-delimited 数据"""
        length = self._read_varint()
        result = self.data[self.pos:self.pos + length]
        self.pos += length
        return result

    def parse_danmaku_seg(self) -> list[Danmaku]:
        """解析 DmSegMobileReply

        顶层结构:
          field 1 (elems): repeated DanmakuElem (length-delimited)

        DanmakuElem 结构:
          field 1: id (int64)
          field 2: progress (int32)
          field 3: mode (int32)
          field 4: fontsize (int32)
          field 5: color (uint32)
          field 6: midHash (string)
          field 7: content (string)
          field 8: ctime (int64)
          field 9: weight (int32)
          field 10: action (string)
          field 11: pool (int32)
        """
        danmaku_list = []

        while self.pos < len(self.data):
            fn, wt = self._read_tag()
            if fn == 1 and wt == 2:
                # 嵌套的 DanmakuElem
                elem_data = self._read_length_delimited()
                inner = ProtobufReader(elem_data)
                d = inner._parse_danmaku_elem()
                if d:
                    danmaku_list.append(d)
            elif wt == 2:
                self._read_length_delimited()  # skip unknown
            elif wt == 0:
                self._read_varint()  # skip unknown varint
            else:
                break

        return danmaku_list

    def _parse_danmaku_elem(self) -> Danmaku | None:
        """解析单个 DanmakuElem 消息"""
        d = Danmaku(
            id=0, progress=0, mode=0, fontsize=25, color=0xFFFFFF,
            mid_hash="", content="", ctime=0, weight=0, pool=0,
        )
        has_content = False

        while self.pos < len(self.data):
            fn, wt = self._read_tag()
            if wt == 0:
                v = self._read_varint()
                if fn == 1:
                    d.id = v
                elif fn == 2:
                    d.progress = v
                elif fn == 3:
                    d.mode = v
                elif fn == 4:
                    d.fontsize = v
                elif fn == 5:
                    d.color = v
                elif fn == 8:
                    d.ctime = v
                elif fn == 9:
                    d.weight = v
                elif fn == 11:
                    d.pool = v
            elif wt == 2:
                v = self._read_length_delimited()
                if fn == 6:
                    d.mid_hash = v.decode("utf-8", errors="replace")
                elif fn == 7:
                    d.content = v.decode("utf-8", errors="replace")
                    has_content = True
                elif fn == 10:
                    d.action = v.decode("utf-8", errors="replace") if len(v) > 0 else ""
                else:
                    pass  # 跳过未知字段
            elif wt == 5:
                self.pos += 4  # 跳过固定32位
            else:
                break

        return d if has_content else None


def fetch_danmaku_segment(oid: int, segment_index: int) -> bytes:
    """获取一个弹幕分段（6分钟一段，protobuf 格式）
    不使用 WBI 签名，避免签名影响数据完整性"""
    client = httpx.Client(
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com",
        },
        timeout=15, trust_env=False,
    )
    resp = client.get(
        "https://api.bilibili.com/x/v2/dm/web/seg.so",
        params={
            "oid": oid,
            "type": 1,
            "segment_index": segment_index,
        },
    )
    return resp.content if resp.status_code == 200 and len(resp.content) > 0 else b""


def _decode_segment(raw: bytes) -> bytes | None:
    """解码弹幕分段数据（可能是原始 protobuf 或 gzip 压缩）"""
    if not raw:
        return None

    # 检查是否是 JSON 错误响应
    if raw[:1] == b"{":
        return None

    # 尝试 gzip 解压
    try:
        return gzip.decompress(raw)
    except gzip.BadGzipFile:
        pass

    # 直接作为 protobuf 处理
    return raw


def fetch_video_danmaku(
    oid: int,
    duration_seconds: int,
    delay: float = 0.6,
    progress_callback=None,
) -> list[Danmaku]:
    """采集视频的全部弹幕

    Args:
        oid: 视频 cid
        duration_seconds: 视频时长（秒）
        delay: 段间延迟（秒，默认 0.6）
        progress_callback: 进度回调 (current, total_segments)
    """
    # 每 6 分钟一个分段，segment_index 从 1 开始
    segment_count = max(1, (duration_seconds + 359) // 360)

    all_danmaku: list[Danmaku] = []
    errors = 0

    for idx in range(1, segment_count + 1):  # 从 1 开始
        try:
            raw = fetch_danmaku_segment(oid, idx)
        except Exception as e:
            errors += 1
            if progress_callback:
                progress_callback(idx, segment_count)
            continue

        decoded = _decode_segment(raw)
        if decoded is None:
            if progress_callback:
                progress_callback(idx, segment_count)
            continue

        reader = ProtobufReader(decoded)
        seg_dms = reader.parse_danmaku_seg()
        all_danmaku.extend(seg_dms)

        if progress_callback:
            progress_callback(idx, segment_count)

        time.sleep(delay)  # 频率控制

    if errors > 0:
        print(f"  ⚠ {errors}/{segment_count} 个弹幕分段获取失败")

    return all_danmaku
