"""
B站登录凭证加载模块（独立工具，可被其它模块直接引用）

凭证读取优先级：
  1. 环境变量 BILI_SESSDATA
  2. 项目根目录（本文件所在目录）的 .sessdata 文件

典型用法：
    from bili_auth import load_sessdata, build_cookie
    sessdata = load_sessdata()                  # 缺失时抛出 SessdataNotFound
    headers["Cookie"] = build_cookie(sessdata)   # 生成 Cookie 头
"""
import os

# 默认指向本文件所在目录，与运行时的工作目录无关
DEFAULT_SESSDATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sessdata")


class SessdataNotFound(RuntimeError):
    """未找到有效的 SESSDATA 凭证时抛出。"""


def load_sessdata(path: str = DEFAULT_SESSDATA_FILE, *, required: bool = True) -> str:
    """
    读取 B站 SESSDATA 凭证。

    先读环境变量 BILI_SESSDATA；取不到再读 path 指定的文件。
    返回去掉首尾空白后的原始内容。

    :param path:     .sessdata 文件路径（默认项目根目录下的 .sessdata）
    :param required: True 时找不到凭证抛 SessdataNotFound；False 时返回空字符串
    """
    # 1) 环境变量优先（便于 CI / 服务器部署，避免明文文件）
    env_val = os.environ.get("BILI_SESSDATA", "").strip()
    if env_val:
        return env_val

    # 2) 回退到本地文件
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                file_val = f.read().strip()
        except OSError as e:
            if required:
                raise SessdataNotFound(f"读取凭证文件 {path} 失败：{e}") from e
            return ""
        if file_val:
            return file_val

    # 3) 两处都没有
    if required:
        raise SessdataNotFound(
            "未找到 B站登录凭证。请任选其一配置：\n"
            "  · 设置环境变量 BILI_SESSDATA=<你的 SESSDATA>；\n"
            f"  · 或在项目根目录创建 {path}，写入 SESSDATA 的值。\n"
            "获取方式：浏览器登录 B站后，从 Cookie 中复制 SESSDATA 字段的值。"
        )
    return ""


def build_cookie(sessdata: str) -> str:
    """
    由 SESSDATA 生成最简 Cookie 头。

    兼容三种写法：纯值、'SESSDATA=xxx'、或已含多字段的完整 cookie 串。
    """
    sessdata = (sessdata or "").strip()
    if "SESSDATA=" in sessdata:
        return sessdata
    return f"SESSDATA={sessdata}"


if __name__ == "__main__":
    # 自检：python bili_auth.py
    try:
        val = load_sessdata()
        masked = (val[:6] + "…" + val[-4:]) if len(val) > 12 else "(较短)"
        print(f"✓ 已加载 SESSDATA（{masked}），长度 {len(val)}")
    except SessdataNotFound as e:
        print(f"✗ {e}")
