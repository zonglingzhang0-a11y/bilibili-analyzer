"""
B站 Wbi 签名模块
实现参数签名算法，为所有需要签名的 API 调用提供 w_rid 和 wts
"""
import hashlib
import time
import urllib.parse

import httpx

from bili_http import BASE_HEADERS, make_client

# Wbi 签名用的固定盐值映射表
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 52, 44, 34,
]


def get_mixin_key(orig: str) -> str:
    """对原始 key 进行重排，截取前 32 位"""
    return "".join(orig[n] for n in MIXIN_KEY_ENC_TAB if n < len(orig))[:32]


class WbiSigner:
    """B站 Wbi 签名器，缓存 key 并自动刷新"""

    def __init__(self):
        self._client = make_client({**BASE_HEADERS, "Cookie": "buvid3=auto"}, timeout=10)
        self._mixin_key: str = ""
        self._last_refresh = 0.0

    def _refresh_key(self):
        """从 B站 nav 接口获取并计算 mixin key"""
        resp = self._client.get("https://api.bilibili.com/x/web-interface/nav")
        data = resp.json()["data"]
        img_url = data["wbi_img"]["img_url"]
        sub_url = data["wbi_img"]["sub_url"]

        # 提取文件名（去掉路径和扩展名）
        img_key = img_url.rsplit("/", 1)[-1].split(".")[0]
        sub_key = sub_url.rsplit("/", 1)[-1].split(".")[0]

        # 拼接后用映射表重排
        self._mixin_key = get_mixin_key(img_key + sub_key)
        self._last_refresh = time.time()

    @property
    def mixin_key(self) -> str:
        """获取当前有效的 mixin_key，超过 30 分钟自动刷新"""
        if not self._mixin_key or time.time() - self._last_refresh > 1800:
            self._refresh_key()
        return self._mixin_key

    def invalidate(self):
        """丢弃缓存的 mixin_key，下次签名时强制重新获取"""
        self._mixin_key = ""

    def sign(self, params: dict) -> dict:
        """对请求参数进行 Wbi 签名，返回添加了 w_rid 和 wts 的新参数字典

        不修改传入的 dict：重试时复用同一份参数，旧的 wts/w_rid 不会混进签名。
        """
        mixin = self.mixin_key
        # 去掉旧签名字段，并按规范过滤 value 中的 "!'()*" 字符
        params = {
            k: "".join(ch for ch in str(v) if ch not in "!'()*")
            for k, v in (params or {}).items()
            if k not in ("wts", "w_rid")
        }
        # wts 需要略早于服务器时间，避免被当作未来时间戳拒绝
        params["wts"] = int(time.time()) - 5
        # 按 key 排序
        sorted_params = sorted(params.items(), key=lambda x: x[0])
        # 构造 query string 并用 unquote 解码（签名要求原始字符）
        query = urllib.parse.urlencode(sorted_params) if sorted_params else ""
        if query:
            query = urllib.parse.unquote(query)
        # MD5(query + mixin_key)
        sign_str = query + mixin
        w_rid = hashlib.md5(sign_str.encode("utf-8")).hexdigest()
        params["w_rid"] = w_rid
        return params

    def signed_get(self, url: str, params: dict = None, **kwargs) -> httpx.Response:
        """发送带 Wbi 签名的 GET 请求"""
        params = self.sign(params or {})
        return self._client.get(url, params=params, **kwargs)

    @property
    def client(self) -> httpx.Client:
        return self._client

    def close(self):
        self._client.close()


# 全局单例
_signer: WbiSigner = None


def get_signer() -> WbiSigner:
    global _signer
    if _signer is None:
        _signer = WbiSigner()
    return _signer
