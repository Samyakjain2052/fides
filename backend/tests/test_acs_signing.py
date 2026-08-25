"""The ACS shared-key signature.

Worth its own tests because a wrong signature fails as 401, which reads exactly
like a wrong key — so the failure gives no hint about which of the several
load-bearing details is wrong. Each of these pins one of them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from app.services.notification_providers import AzureCommunicationEmailProvider as Acs

KEY = base64.b64encode(b"a-shared-key-of-some-length").decode()
URL = "https://demo.india.communication.azure.com/emails:send?api-version=2023-03-31"
DATE = "Mon, 25 Aug 2026 10:00:00 GMT"
BODY = b'{"senderAddress":"a@b.example","recipients":{"to":[{"address":"c@d.example"}]}}'


def _headers(**over):
    kw = {"method": "POST", "url": URL, "body": BODY, "access_key": KEY, "date": DATE}
    kw.update(over)
    return Acs._sign(**kw)


def test_the_signature_matches_a_hand_computed_one():
    """The whole scheme, recomputed independently rather than trusting the code
    under test to agree with itself."""
    content_hash = base64.b64encode(hashlib.sha256(BODY).digest()).decode()
    string_to_sign = (
        f"POST\n/emails:send?api-version=2023-03-31\n"
        f"{DATE};demo.india.communication.azure.com;{content_hash}"
    )
    expected = base64.b64encode(
        hmac.new(base64.b64decode(KEY), string_to_sign.encode(), hashlib.sha256).digest()
    ).decode()

    h = _headers()
    assert f"Signature={expected}" in h["Authorization"]
    assert h["x-ms-content-sha256"] == content_hash
    assert h["x-ms-date"] == DATE


def test_the_key_is_decoded_before_use():
    """The access key is base64. Signing the base64 *text* produces a
    valid-looking signature that ACS always rejects, and that mistake is
    invisible without this test."""
    h = _headers()
    wrong = base64.b64encode(
        hmac.new(
            KEY.encode(),  # the un-decoded key — the bug
            f"POST\n/emails:send?api-version=2023-03-31\n{DATE};"
            f"demo.india.communication.azure.com;"
            f"{base64.b64encode(hashlib.sha256(BODY).digest()).decode()}".encode(),
            hashlib.sha256,
        ).digest()
    ).decode()
    assert f"Signature={wrong}" not in h["Authorization"]


def test_the_query_string_is_part_of_what_is_signed():
    """Dropping `?api-version=` from the canonical string is an easy slip and
    authenticates as a 401 rather than as a bad request."""
    with_q = _headers()["Authorization"]
    without_q = _headers(url="https://demo.india.communication.azure.com/emails:send")
    assert with_q != without_q["Authorization"]


def test_a_different_body_gives_a_different_signature():
    """The body hash has to actually cover the body, or a signature could be
    replayed against different content."""
    assert _headers()["Authorization"] != _headers(body=b'{"different":true}')["Authorization"]


def test_the_signed_headers_are_the_three_acs_checks():
    auth = _headers()["Authorization"]
    assert auth.startswith("HMAC-SHA256 SignedHeaders=x-ms-date;host;x-ms-content-sha256&Signature=")


async def test_sms_is_refused_without_calling_out():
    """SMS is unimplemented. Refusing it non-retryably keeps it out of the retry
    queue instead of failing forever."""
    r = await Acs().send(to="a@b.example", subject="s", body="b", channel="sms")
    assert not r.ok and not r.retryable
    assert "email only" in r.error
