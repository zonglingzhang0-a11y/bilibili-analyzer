import httpx
import pytest

import bili_auth


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeLoginClient:
    """生成二维码 → 未扫码 → 已扫码 → 成功（成功时写入 Cookie）"""

    def __init__(self, poll_codes, sessdata="abc%2C123%2Axyz"):
        self.cookies = httpx.Cookies()
        self.poll_codes = list(poll_codes)
        self.sessdata = sessdata

    def get(self, url, params=None):
        if url == bili_auth.QR_GENERATE_URL:
            return _FakeResponse({"code": 0, "data": {"url": "https://example/qr", "qrcode_key": "k"}})
        code = self.poll_codes.pop(0)
        if code == bili_auth.QR_SUCCESS:
            self.cookies.set("SESSDATA", self.sessdata, domain=".bilibili.com")
        return _FakeResponse({"code": 0, "data": {"code": code, "url": ""}})

    def close(self):
        pass


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    monkeypatch.setattr(bili_auth.time, "sleep", lambda *_: None)
    monkeypatch.delenv("BILI_SESSDATA", raising=False)


def test_qr_login_writes_new_sessdata(tmp_path):
    path = tmp_path / ".sessdata"
    path.write_text("旧值", encoding="utf-8")
    client = _FakeLoginClient([bili_auth.QR_WAITING, bili_auth.QR_SCANNED, bili_auth.QR_SUCCESS])

    assert bili_auth.qr_login(str(path), client=client, show_qr=lambda url: None)
    assert path.read_text(encoding="utf-8") == "abc%2C123%2Axyz"


def test_qr_login_expired_keeps_old_file(tmp_path):
    path = tmp_path / ".sessdata"
    path.write_text("旧值", encoding="utf-8")
    client = _FakeLoginClient([bili_auth.QR_WAITING, bili_auth.QR_EXPIRED])

    assert not bili_auth.qr_login(str(path), client=client, show_qr=lambda url: None)
    assert path.read_text(encoding="utf-8") == "旧值"


def test_sessdata_fallback_from_callback_url():
    client = _FakeLoginClient([])
    url = "https://passport.biligame.com/crossDomain?DedeUserID=1&SESSDATA=abc%2C123%2Axyz&bili_jct=x"
    assert bili_auth._extract_sessdata(client, {"url": url}) == "abc%2C123%2Axyz"
