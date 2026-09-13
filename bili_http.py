"""
B站请求公共配置：统一的 User-Agent、请求头和 httpx Client 工厂

更换 UA 或调整请求头只需要改这里。
"""
import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# 普通接口与静态资源（期号、视频信息、封面、精灵图、弹幕分段）
BASE_HEADERS = {
    "User-Agent": USER_AGENT,
    "Referer": "https://www.bilibili.com",
}

# 评论接口：模拟完整浏览器请求头，降低 412 概率。
# 不手动声明 Accept-Encoding：未安装 brotli 时 httpx 无法解压 br 响应，
# 会导致 JSON 解析失败，交给 httpx 按已安装的解码器自动协商。
BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Origin": "https://www.bilibili.com",
    "Referer": "https://www.bilibili.com/",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "Connection": "keep-alive",
}


def make_client(headers: dict = None, timeout: float = 15, **kwargs) -> httpx.Client:
    """创建 httpx Client（默认 BASE_HEADERS，不读取系统代理环境变量）"""
    return httpx.Client(headers=headers or BASE_HEADERS, timeout=timeout,
                        trust_env=False, **kwargs)
