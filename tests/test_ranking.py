import pytest

import ranking


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSigner:
    def __init__(self):
        self.params = []

    def signed_get(self, url, params=None):
        self.params.append(dict(params))
        number = params.get("number", 1)  # 真实接口不带 number 时返回第 1 期
        return _FakeResponse({"code": 0, "data": {
            "config": {"number": number, "name": f"第{number}期"},
            "list": [],
        }})


@pytest.fixture
def signer(monkeypatch):
    fake = _FakeSigner()
    monkeypatch.setattr(ranking, "get_signer", lambda: fake)
    monkeypatch.setattr(ranking, "fetch_weekly_series",
                        lambda: [{"number": 1}, {"number": 390}, {"number": 389}])
    ranking._fetch_series_one.cache_clear()
    yield fake
    ranking._fetch_series_one.cache_clear()


def test_latest_issue_is_requested_with_explicit_number(signer):
    ranking.fetch_weekly_videos(None)
    assert signer.params == [{"number": 390}]


def test_series_info_and_videos_share_one_request(signer):
    assert ranking.get_series_info(390)["number"] == 390
    ranking.fetch_weekly_videos(390)
    assert len(signer.params) == 1
