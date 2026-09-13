import hashlib
import time
import urllib.parse

from wbi import WbiSigner


def _signer() -> WbiSigner:
    signer = WbiSigner()
    signer._mixin_key = "a" * 32
    signer._last_refresh = time.time()
    return signer


def _expected_w_rid(params: dict, mixin: str = "a" * 32) -> str:
    query = urllib.parse.unquote(urllib.parse.urlencode(sorted(params.items())))
    return hashlib.md5((query + mixin).encode()).hexdigest()


def test_sign_does_not_mutate_input():
    params = {"number": 1}
    _signer().sign(params)
    assert params == {"number": 1}


def test_resigning_signed_params_ignores_old_signature():
    signer = _signer()
    signed = signer.sign({"number": 1})
    again = signer.sign(signed)
    assert again["w_rid"] == _expected_w_rid({"number": "1", "wts": again["wts"]})


def test_sign_filters_reserved_characters():
    assert _signer().sign({"k": "a!b'(c)*"})["k"] == "abc"
