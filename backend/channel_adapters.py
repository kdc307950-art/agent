"""企业微信 / 钉钉渠道 Webhook 适配器 —— 外部消息验签、解密与归一化。

职责：
    - 企业微信：URL 验证（verify_url）与消息验签解密（verify_and_parse），
      支持 AES-CBC 解密 + 签名校验 + 时间戳防重放
    - 钉钉：回调签名校验（HMAC-SHA256 + timestamp）与 JSON 解析
    - 把两类渠道消息统一归一化为 NormalizedChannelEvent，供路由层建单

关键设计：
    - 验签统一走 _verify_timestamp（防重放窗口）→ 签名比对（hmac.compare_digest
      防时序攻击）→ 内容解析三道关卡
    - 非文本事件（enter_agent / location / subscribe 等）抛 IgnoreWebhookEvent，
      由路由层 ACK 200 但不建单、不落库，避免渠道方无限重试
    - 企业微信 _decrypt 严格校验 PKCS#7 填充、消息长度与 CorpID 接收方，
      任何一步不符即拒绝，防止恶意密文注入
    - 租户来自服务端配置（构造时传入），不接受请求参数中的 tenant_id
"""

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
    """Webhook 验签/解密/解析失败（签名错误、过期、格式非法等）。

    继承 ValueError，路由层捕获后应返回 4xx 拒绝，不做业务处理。
    """

    pass


class IgnoreWebhookEvent(Exception):
    """企业微信事件消息（enter_agent / location / subscribe 等）——验签通过但非文本消息。

    路由层捕获后应返回 200 ACK（避免企业微信重试），不建单、不写 inbound_events。
    """

    def __init__(self, event: str) -> None:
        super().__init__(f"忽略企业微信事件消息: {event}")
        self.event = event


@dataclass(frozen=True, slots=True)
class NormalizedChannelEvent:
    """渠道事件统一归一化结果（不可变、紧凑存储）。

    字段：
        tenant_id        事件归属租户（来自适配器配置）
        channel          渠道名（"wecom" / "dingtalk"）
        external_event_id 渠道侧事件唯一 ID（用于幂等去重）
        external_ticket_id 渠道侧工单 ID（本实现暂为 None）
        requester_id     发起人渠道 ID（工单客户标识）
        title            标题（取内容前 120 字符）
        content          消息正文
        payload          原始消息结构（XML 子节点 / 钉钉 JSON），供扩展使用
    """

    tenant_id: str
    channel: str
    external_event_id: str
    external_ticket_id: str | None
    requester_id: str
    title: str
    content: str
    payload: dict[str, Any]


def _verify_timestamp(timestamp: str, *, now: int | None, replay_window_seconds: int) -> int:
    """校验渠道时间戳落在防重放窗口内，返回原始时间戳数值。

    参数：now 为注入的当前时间（测试用），replay_window_seconds 为允许的
    时间偏差（秒）。
    抛错：WebhookVerificationError —— 时间戳非法或超出窗口。
    设计：兼容秒/毫秒两种单位（>10_000_000_000 视为毫秒）；窗口外的
    请求一律拒绝，防止重放攻击。
    """
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
    """安全解析 XML：先拒绝 DOCTYPE/ENTITY 声明（防 XXE），再解析。

    抛错：WebhookVerificationError —— 含禁止声明或 XML 格式非法。
    """
    upper = value.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        # 外部实体可能导致 XXE 读取本地文件或 SSRF，直接拒绝。
        raise WebhookVerificationError("Webhook XML 包含禁止声明")
    try:
        return ET.fromstring(value)
    except ET.ParseError as exc:
        raise WebhookVerificationError("Webhook XML 无效") from exc


def _xml_text(root: ET.Element, name: str, *, required: bool = True) -> str | None:
    """取 XML 子节点文本并去空白；required=True 时缺失即抛错。

    返回：去空白后的字符串；节点缺失或为空时返回 None（required=False 时）。
    """
    element = root.find(name)
    value = None if element is None else (element.text or "").strip()
    if required and not value:
        raise WebhookVerificationError(f"Webhook 缺少字段: {name}")
    return value or None


class WeComWebhookAdapter:
    """企业微信回调适配器：URL 验证 + 消息验签解密 + 归一化。

    构造参数：tenant_id 归属租户；token 回调 Token；encoding_aes_key
    43 字符 AES 密钥（Base64 解码后 32 字节）；corp_id 企业 ID；
    replay_window_seconds 防重放窗口（默认 300 秒）。
    """

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
        # corp_id 预编码为 UTF-8 字节，解密后用于校验接收方。
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
        """验签、解密并归一化企业微信回调消息（POST 消息入口）。

        参数：body 原始请求体；timestamp/nonce/signature 渠道回调签名要素；
            now 可选注入时间（测试用）。
        返回：NormalizedChannelEvent；非文本事件抛 IgnoreWebhookEvent。
        抛错：WebhookVerificationError —— 时间戳过期、签名不符、XML/密文非法。
        设计：外层 XML 取 Encrypt 密文验签 → AES-CBC 解密 → 内层 XML 解析；
        文本消息（MsgType=text）才进入受理流程，事件消息直接忽略。
        """
        _verify_timestamp(timestamp, now=now, replay_window_seconds=self.replay_window_seconds)
        try:
            outer = _safe_xml(body.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise WebhookVerificationError("Webhook body 不是 UTF-8") from exc
        encrypted = _xml_text(outer, "Encrypt")
        if not encrypted:
            raise WebhookVerificationError("企业微信回调缺少 Encrypt 内容")
        expected = hashlib.sha1(
            "".join(sorted((self.token, timestamp, nonce, encrypted))).encode("utf-8")
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise WebhookVerificationError("企业微信签名无效")
        plaintext = self._decrypt(encrypted)
        message = _safe_xml(plaintext.decode("utf-8"))
        # 区分文本消息与事件消息：enter_agent / location / subscribe 等事件不建单，
        # 由路由层 ACK 200 并忽略；只有文本消息（MsgType=text，含 Content）进入受理。
        msg_type = (_xml_text(message, "MsgType", required=False) or "").strip().lower()
        event = _xml_text(message, "Event", required=False)
        if msg_type == "event" or event:
            raise IgnoreWebhookEvent(event or msg_type or "unknown")
        # 事件 ID 优先取 MsgId/EventId；缺失时退回明文 SHA-256，保证幂等键始终存在。
        event_id = _xml_text(message, "MsgId", required=False) or _xml_text(
            message, "EventId", required=False
        )
        if not event_id:
            event_id = hashlib.sha256(plaintext).hexdigest()
        requester = _xml_text(message, "FromUserName")
        content = _xml_text(message, "Content", required=False)
        if not content:
            raise WebhookVerificationError("企业微信文本消息缺少 Content")
        return NormalizedChannelEvent(
            tenant_id=self.tenant_id,
            channel="wecom",
            external_event_id=event_id,
            external_ticket_id=None,
            requester_id=requester or "",
            title=content[:120],
            content=content,
            # 原始子节点快照：key=节点名，value=节点文本，供下游扩展消费。
            payload={child.tag: child.text or "" for child in message},
        )

    def _decrypt(self, encrypted: str) -> bytes:
        """解密企业微信 AES-CBC 密文，返回纯消息字节（不含填充与头部）。

        密文格式（PKCS#7 填充）：random(16B) + msg_len(4B 大端) + msg + corp_id。
        逐层校验：Base64 合法性 → 长度 16 对齐 → 填充有效性 → 长度字段
        与 corp_id 接收方匹配，任何一步失败即抛 WebhookVerificationError。
        """
        try:
            ciphertext = base64.b64decode(encrypted, validate=True)
        except ValueError as exc:
            raise WebhookVerificationError("企业微信密文无效") from exc
        if not ciphertext or len(ciphertext) % 16:
            raise WebhookVerificationError("企业微信密文长度无效")
        decryptor = Cipher(algorithms.AES(self.key), modes.CBC(self.key[:16])).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        # PKCS#7 填充校验：填充字节数必须为 1~32 且末尾连续重复。
        pad = padded[-1]
        if pad < 1 or pad > 32 or padded[-pad:] != bytes([pad]) * pad:
            raise WebhookVerificationError("企业微信填充无效")
        value = padded[:-pad]
        if len(value) < 20:
            raise WebhookVerificationError("企业微信明文长度无效")
        # 明文头部：16 字节随机串 + 4 字节大端消息长度。
        message_length = struct.unpack("!I", value[16:20])[0]
        end = 20 + message_length
        if end > len(value):
            raise WebhookVerificationError("企业微信消息长度无效")
        message, receiver = value[20:end], value[end:]
        # 尾部必须为配置的 CorpID，防止密文被跨企业重放。
        if not hmac.compare_digest(receiver, self.corp_id):
            raise WebhookVerificationError("企业微信 CorpID 不匹配")
        return message


class DingTalkWebhookAdapter:
    """钉钉回调适配器：HMAC-SHA256 签名校验 + JSON 解析归一化。

    构造参数：tenant_id 归属租户；app_secret 应用密钥；
    replay_window_seconds 防重放窗口（默认 300 秒）。
    签名规则：base64(hmac_sha256(secret, timestamp + "\\n" + secret))。
    """

    def __init__(
        self, *, tenant_id: str, app_secret: str, replay_window_seconds: int = 300
    ) -> None:
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
        """验签并解析钉钉回调（POST 消息入口）。

        参数：body 原始 JSON 请求体；timestamp/signature 回调签名要素；
            now 可选注入时间（测试用）。
        返回：NormalizedChannelEvent。
        抛错：WebhookVerificationError —— 时间戳过期、签名不符、JSON 非法、
            缺少事件/用户/内容字段。
        """
        timestamp_value = _verify_timestamp(
            timestamp,
            now=now,
            replay_window_seconds=self.replay_window_seconds,
        )
        # 钉钉签名串：timestamp + "\n" + secret，再做 HMAC-SHA256 并 Base64。
        signing = f"{timestamp_value}\n{self.secret.decode('utf-8')}".encode()
        expected = base64.b64encode(hmac.new(self.secret, signing, hashlib.sha256).digest()).decode(
            "ascii"
        )
        # 回调里的 signature 经过 URL 编码，需 unquote 后与期望值恒时比较。
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
        # 钉钉文本消息的正文在 text.content 中；事件消息退回到 EventType 字段。
        text_value = payload.get("text")
        content = (
            text_value.get("content") if isinstance(text_value, dict) else payload.get("content")
        )
        content = str(content or payload.get("EventType") or "").strip()
        # 事件 ID、发起人、内容三者缺一不可，否则拒绝受理。
        if not event_id or not requester or not content:
            raise WebhookVerificationError("钉钉 Webhook 缺少事件、用户或内容字段")
        return NormalizedChannelEvent(
            tenant_id=self.tenant_id,
            channel="dingtalk",
            external_event_id=event_id,
            external_ticket_id=None,
            requester_id=requester or "",
            title=content[:120],
            content=content,
            payload=payload,
        )
