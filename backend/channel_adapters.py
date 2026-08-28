"""Verified WeCom and DingTalk webhook adapters."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import struct
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class WebhookVerificationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class NormalizedChannelEvent:
    tenant_id: str
    channel: str
    external_event_id: str
    external_ticket_id: str | None
    requester_id: str
    title: str
    content: str
    payload: dict[str, Any]


def _verify_timestamp(timestamp: str, *, now: int | None, replay_window_seconds: int) -> int:
    try:
        value = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise WebhookVerificationError("Webhook timestamp 无效") from exc
    reference = int(time.time()) if now is None else int(now)
    comparison_value = value // 1000 if value > 10_000_000_000 else value
    if abs(reference - comparison_value) > replay_window_seconds:
        raise WebhookVerificationError("Webhook timestamp 已过期")
    return value


def _safe_xml(value: str) -> ET.Element:
    upper = value.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise WebhookVerificationError("Webhook XML 包含禁止声明")
    try:
        return ET.fromstring(value)
    except ET.ParseError as exc:
        raise WebhookVerificationError("Webhook XML 无效") from exc


def _xml_text(root: ET.Element, name: str, *, required: bool = True) -> str | None:
    element = root.find(name)
    value = None if element is None else (element.text or "").strip()
    if required and not value:
        raise WebhookVerificationError(f"Webhook 缺少字段: {name}")
    return value or None


class WeComWebhookAdapter:
    def __init__(
        self,
        *,
        tenant_id: str,
        token: str,
        encoding_aes_key: str,
        corp_id: str,
        replay_window_seconds: int = 300,
    ) -> None:
        if len(encoding_aes_key) != 43:
            raise ValueError("企业微信 EncodingAESKey 必须为 43 个字符")
        try:
            self.key = base64.b64decode(encoding_aes_key + "=", validate=True)
        except ValueError as exc:
            raise ValueError("企业微信 EncodingAESKey 无效") from exc
        if len(self.key) != 32:
            raise ValueError("企业微信 AES 密钥必须解码为 32 字节")
        self.tenant_id = tenant_id
        self.token = token
        self.corp_id = corp_id.encode("utf-8")
        self.replay_window_seconds = replay_window_seconds

    def verify_url(
        self,
        *,
        timestamp: str,
        nonce: str,
        signature: str,
        echostr: str,
        now: int | None = None,
    ) -> str:
        """企业微信后台「保存回调 URL」时的 GET 验证。

        只验证签名并解密 echostr 回显明文，不创建工单、不访问业务表、
        不调用 LLM。与 POST 消息共用 _verify_timestamp / _decrypt，
        租户来自服务端配置，不接受请求参数中的 tenant_id。
        """
        _verify_timestamp(timestamp, now=now, replay_window_seconds=self.replay_window_seconds)
        expected = hashlib.sha1(
            "".join(sorted((self.token, timestamp, nonce, echostr))).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise WebhookVerificationError("企业微信签名无效")
        plaintext = self._decrypt(echostr)
        return plaintext.decode("utf-8")

    def verify_and_parse(
        self,
        body: bytes,
        *,
        timestamp: str,
        nonce: str,
        signature: str,
        now: int | None = None,
    ) -> NormalizedChannelEvent:
        _verify_timestamp(timestamp, now=now, replay_window_seconds=self.replay_window_seconds)
        try:
            outer = _safe_xml(body.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise WebhookVerificationError("Webhook body 不是 UTF-8") from exc
        encrypted = _xml_text(outer, "Encrypt")
        expected = hashlib.sha1(
            "".join(sorted((self.token, timestamp, nonce, encrypted))).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise WebhookVerificationError("企业微信签名无效")
        plaintext = self._decrypt(encrypted)
        message = _safe_xml(plaintext.decode("utf-8"))
        event_id = _xml_text(message, "MsgId", required=False) or _xml_text(message, "EventId", required=False)
        if not event_id:
            event_id = hashlib.sha256(plaintext).hexdigest()
        requester = _xml_text(message, "FromUserName")
        content = _xml_text(message, "Content", required=False) or _xml_text(message, "Event", required=False) or "企业微信事件"
        return NormalizedChannelEvent(
            tenant_id=self.tenant_id,
            channel="wecom",
            external_event_id=event_id,
            external_ticket_id=None,
            requester_id=requester,
            title=content[:120],
            content=content,
            payload={child.tag: child.text or "" for child in message},
        )

    def _decrypt(self, encrypted: str) -> bytes:
        try:
            ciphertext = base64.b64decode(encrypted, validate=True)
        except ValueError as exc:
            raise WebhookVerificationError("企业微信密文无效") from exc
        if not ciphertext or len(ciphertext) % 16:
            raise WebhookVerificationError("企业微信密文长度无效")
        decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        pad = padded[-1]
        if pad < 1 or pad > 32 or padded[-pad:] != bytes([pad]) * pad:
            raise WebhookVerificationError("企业微信填充无效")
        value = padded[:-pad]
        if len(value) < 20:
            raise WebhookVerificationError("企业微信明文长度无效")
        message_length = struct.unpack("!I", value[16:20])[0]
        end = 20 + message_length
        if end > len(value):
            raise WebhookVerificationError("企业微信消息长度无效")
        message, receiver = value[20:end], value[end:]
        if not hmac.compare_digest(receiver, self.corp_id):
            raise WebhookVerificationError("企业微信 CorpID 不匹配")
        return message


class DingTalkWebhookAdapter:
    def __init__(self, *, tenant_id: str, app_secret: str, replay_window_seconds: int = 300) -> None:
        self.tenant_id = tenant_id
        self.secret = app_secret.encode("utf-8")
        self.replay_window_seconds = replay_window_seconds

    def verify_and_parse(
        self,
        body: bytes,
        *,
        timestamp: str,
        signature: str,
        now: int | None = None,
    ) -> NormalizedChannelEvent:
        timestamp_value = _verify_timestamp(
            timestamp,
            now=now,
            replay_window_seconds=self.replay_window_seconds,
        )
        signing = f"{timestamp_value}\n{self.secret.decode('utf-8')}".encode("utf-8")
        expected = base64.b64encode(hmac.new(self.secret, signing, hashlib.sha256).digest()).decode("ascii")
        if not hmac.compare_digest(expected, unquote(signature)):
            raise WebhookVerificationError("钉钉签名无效")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WebhookVerificationError("钉钉 Webhook JSON 无效") from exc
        if not isinstance(payload, dict):
            raise WebhookVerificationError("钉钉 Webhook 必须是 JSON 对象")
        event_id = str(payload.get("msgId") or payload.get("eventId") or "").strip()
        requester = str(payload.get("senderStaffId") or payload.get("senderId") or "").strip()
        text_value = payload.get("text")
        content = text_value.get("content") if isinstance(text_value, dict) else payload.get("content")
        content = str(content or payload.get("EventType") or "").strip()
        if not event_id or not requester or not content:
            raise WebhookVerificationError("钉钉 Webhook 缺少事件、用户或内容字段")
        return NormalizedChannelEvent(
            tenant_id=self.tenant_id,
            channel="dingtalk",
            external_event_id=event_id,
            external_ticket_id=None,
            requester_id=requester,
            title=content[:120],
            content=content,
            payload=payload,
        )
