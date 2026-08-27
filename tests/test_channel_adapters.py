import base64
import hashlib
import hmac
import json
import struct
from urllib.parse import quote

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from backend.channel_adapters import (
    DingTalkWebhookAdapter,
    WeComWebhookAdapter,
    WebhookVerificationError,
)


WECOM_KEY = b"0123456789abcdef0123456789abcdef"
WECOM_ENCODING_KEY = base64.b64encode(WECOM_KEY).decode("ascii").rstrip("=")


def encrypt_wecom(message: bytes, corp_id: str) -> str:
    raw = b"0123456789abcdef" + struct.pack("!I", len(message)) + message + corp_id.encode("utf-8")
    pad = 32 - len(raw) % 32
    padded = raw + bytes([pad]) * pad
    encryptor = Cipher(algorithms.AES(WECOM_KEY), modes.CBC(WECOM_KEY[:16])).encryptor()
    return base64.b64encode(encryptor.update(padded) + encryptor.finalize()).decode("ascii")


def wecom_body(*, corp_id="corp-1"):
    inner = (
        "<xml><FromUserName>user-1</FromUserName><MsgId>msg-1</MsgId>"
        "<Content>SSO login failed</Content></xml>"
    ).encode("utf-8")
    encrypted = encrypt_wecom(inner, corp_id)
    return f"<xml><Encrypt>{encrypted}</Encrypt></xml>".encode(), encrypted


def test_wecom_verifies_signature_decrypts_and_normalizes():
    body, encrypted = wecom_body()
    timestamp = "1700000000"
    nonce = "nonce-1"
    token = "token-1"
    signature = hashlib.sha1("".join(sorted((token, timestamp, nonce, encrypted))).encode()).hexdigest()
    adapter = WeComWebhookAdapter(
        tenant_id="tenant-a",
        token=token,
        encoding_aes_key=WECOM_ENCODING_KEY,
        corp_id="corp-1",
    )

    event = adapter.verify_and_parse(
        body,
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        now=1700000000,
    )

    assert event.tenant_id == "tenant-a"
    assert event.channel == "wecom"
    assert event.external_event_id == "msg-1"
    assert event.requester_id == "user-1"
    assert event.content == "SSO login failed"


def test_wecom_rejects_bad_signature_expired_timestamp_corp_and_dangerous_xml():
    body, encrypted = wecom_body()
    timestamp = "1700000000"
    nonce = "nonce-1"
    token = "token-1"
    signature = hashlib.sha1("".join(sorted((token, timestamp, nonce, encrypted))).encode()).hexdigest()
    adapter = WeComWebhookAdapter(
        tenant_id="tenant-a",
        token=token,
        encoding_aes_key=WECOM_ENCODING_KEY,
        corp_id="corp-1",
        replay_window_seconds=60,
    )
    with pytest.raises(WebhookVerificationError, match="签名"):
        adapter.verify_and_parse(body, timestamp=timestamp, nonce=nonce, signature="bad", now=1700000000)
    with pytest.raises(WebhookVerificationError, match="过期"):
        adapter.verify_and_parse(body, timestamp=timestamp, nonce=nonce, signature=signature, now=1700001000)

    wrong_body, wrong_encrypted = wecom_body(corp_id="other-corp")
    wrong_signature = hashlib.sha1(
        "".join(sorted((token, timestamp, nonce, wrong_encrypted))).encode()
    ).hexdigest()
    with pytest.raises(WebhookVerificationError, match="CorpID"):
        adapter.verify_and_parse(
            wrong_body,
            timestamp=timestamp,
            nonce=nonce,
            signature=wrong_signature,
            now=1700000000,
        )
    with pytest.raises(WebhookVerificationError, match="禁止声明"):
        adapter.verify_and_parse(
            b"<!DOCTYPE x [<!ENTITY y 'z'>]><xml></xml>",
            timestamp=timestamp,
            nonce=nonce,
            signature="bad",
            now=1700000000,
        )


def dingtalk_signature(timestamp: str, secret: str) -> str:
    value = hmac.new(
        secret.encode(),
        f"{timestamp}\n{secret}".encode(),
        hashlib.sha256,
    ).digest()
    return base64.b64encode(value).decode()


def test_dingtalk_verifies_signature_and_normalizes_millisecond_timestamp():
    secret = "secret-1"
    timestamp = "1700000000000"
    body = json.dumps(
        {
            "msgId": "msg-1",
            "senderStaffId": "user-1",
            "text": {"content": "Cannot access VPN"},
        },
        separators=(",", ":"),
    ).encode()
    adapter = DingTalkWebhookAdapter(tenant_id="tenant-a", app_secret=secret)

    event = adapter.verify_and_parse(
        body,
        timestamp=timestamp,
        signature=quote(dingtalk_signature(timestamp, secret), safe=""),
        now=1700000000,
    )

    assert event.tenant_id == "tenant-a"
    assert event.channel == "dingtalk"
    assert event.external_event_id == "msg-1"
    assert event.requester_id == "user-1"
    assert event.content == "Cannot access VPN"


def test_dingtalk_rejects_bad_signature_expired_and_missing_fields():
    adapter = DingTalkWebhookAdapter(
        tenant_id="tenant-a",
        app_secret="secret-1",
        replay_window_seconds=60,
    )
    valid_body = b'{"msgId":"m","senderStaffId":"u","content":"help"}'
    timestamp = "1700000000000"
    with pytest.raises(WebhookVerificationError, match="签名"):
        adapter.verify_and_parse(valid_body, timestamp=timestamp, signature="bad", now=1700000000)
    with pytest.raises(WebhookVerificationError, match="过期"):
        adapter.verify_and_parse(
            valid_body,
            timestamp=timestamp,
            signature=dingtalk_signature(timestamp, "secret-1"),
            now=1700001000,
        )
    missing = b'{"msgId":"m"}'
    with pytest.raises(WebhookVerificationError, match="缺少"):
        adapter.verify_and_parse(
            missing,
            timestamp=timestamp,
            signature=dingtalk_signature(timestamp, "secret-1"),
            now=1700000000,
        )
