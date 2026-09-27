"""
B站登录凭证加载模块（独立工具，可被其它模块直接引用）

凭证读取优先级：
  1. 环境变量 BILI_SESSDATA
  2. 项目根目录（本文件所在目录）的 .sessdata 文件

典型用法：
    from bili_auth import load_sessdata, build_cookie
    sessdata = load_sessdata()                  # 缺失时抛出 SessdataNotFound
    headers["Cookie"] = build_cookie(sessdata)   # 生成 Cookie 头

命令行：
    python bili_auth.py            # 自检：是否配置了凭证
    python bili_auth.py --login    # 扫码登录：用 B站 App 扫码，自动写入 .sessdata
"""
import os
import sys
import tempfile
import time
import urllib.parse

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


def save_sessdata(value: str, path: str = DEFAULT_SESSDATA_FILE):
    """原子写入凭证文件（先写临时文件再替换，避免写到一半留下残缺内容）"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(value.strip())
    os.replace(tmp, path)


# ── 扫码登录 ────────────────────────────────────────────

QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
QR_SUCCESS, QR_EXPIRED, QR_SCANNED, QR_WAITING = 0, 86038, 86090, 86101


def _show_qr(url: str):
    """在终端打印二维码，同时保存成图片并打开（终端字体不支持方块字符时用图片扫）"""
    import segno
    qr = segno.make(url, error="m")
    try:
        qr.terminal(compact=True)
    except Exception:
        pass
    image_path = os.path.join(tempfile.gettempdir(), "bilibili_login_qr.png")
    qr.save(image_path, scale=8, border=4)
    print(f"二维码图片: {image_path}")
    if sys.platform == "win32":
        os.startfile(image_path)
    return image_path


def _extract_sessdata(client, poll_data: dict) -> str:
    """登录成功后从响应 Cookie 中取 SESSDATA；取不到时从回调链接的参数里取"""
    for cookie in client.cookies.jar:
        if cookie.name == "SESSDATA" and cookie.value:
            return cookie.value
    query = urllib.parse.urlparse(poll_data.get("url", "")).query
    values = urllib.parse.parse_qs(query).get("SESSDATA", [])
    # parse_qs 会解码，这里重新编码成浏览器 Cookie 里的形式（如逗号为 %2C）
    return urllib.parse.quote(values[0], safe="") if values else ""


def qr_login(path: str = DEFAULT_SESSDATA_FILE, timeout: float = 180,
             poll_interval: float = 2.0, client=None, show_qr=_show_qr) -> bool:
    """扫码登录：用 B站 App 扫码并确认后，把新的 SESSDATA 写入 path

    凭证只写入文件，不会打印到屏幕。二维码约 3 分钟内有效。

    Returns:
        是否登录成功
    """
    from bili_http import make_client
    own_client = client is None
    client = client or make_client()
    image_path = None
    try:
        data = client.get(QR_GENERATE_URL).json()["data"]
        image_path = show_qr(data["url"])
        print("请用 B站 App 扫描二维码，并在手机上确认登录……", flush=True)

        deadline = time.time() + timeout
        scanned = False
        while time.time() < deadline:
            time.sleep(poll_interval)
            poll = client.get(QR_POLL_URL, params={"qrcode_key": data["qrcode_key"]}).json()["data"]
            code = poll.get("code")
            if code == QR_SCANNED and not scanned:
                print("已扫码，请在手机上点击确认……", flush=True)
                scanned = True
            elif code == QR_EXPIRED:
                print("✗ 二维码已失效，请重新运行 python bili_auth.py --login")
                return False
            elif code == QR_SUCCESS:
                sessdata = _extract_sessdata(client, poll)
                if not sessdata:
                    print("✗ 登录成功但没有拿到 SESSDATA，请改用手动复制的方式")
                    return False
                save_sessdata(sessdata, path)
                print(f"✓ 登录成功，新凭证已写入 {path}")
                if os.environ.get("BILI_SESSDATA", "").strip():
                    print("⚠️ 检测到环境变量 BILI_SESSDATA，它的优先级高于文件，"
                          "请删除该环境变量，否则程序仍会使用旧凭证")
                return True
        print("✗ 等待超时，请重新运行 python bili_auth.py --login")
        return False
    finally:
        if own_client:
            client.close()
        if image_path and os.path.exists(image_path):
            try:
                os.remove(image_path)
            except OSError:
                pass


if __name__ == "__main__":
    if "--login" in sys.argv[1:]:
        if sys.stdout.encoding != "utf-8":
            sys.stdout.reconfigure(encoding="utf-8")
        ok = qr_login()
        if ok:
            from comments import check_login
            print("登录状态检查:", "✓ 已登录" if check_login() else "✗ 仍未登录，请稍后重试")
        sys.exit(0 if ok else 1)

    # 自检：python bili_auth.py
    try:
        val = load_sessdata()
        masked = (val[:6] + "…" + val[-4:]) if len(val) > 12 else "(较短)"
        print(f"✓ 已加载 SESSDATA（{masked}），长度 {len(val)}")
    except SessdataNotFound as e:
        print(f"✗ {e}")
