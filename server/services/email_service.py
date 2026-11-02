"""Controlled SMTP delivery and connection checks for account email codes."""
from __future__ import annotations

import logging
import math
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any

from core import errors
from core.config import get_settings
from core.utils import is_valid_email_address

logger = logging.getLogger(__name__)


def configured() -> bool:
    settings = get_settings()
    return bool(settings.mail_host.strip() and settings.mail_from.strip())


def status() -> dict[str, object]:
    """Return non-secret delivery settings for local diagnostics."""
    settings = get_settings()
    host = settings.mail_host.strip()
    sender = settings.mail_from.strip()
    return {
        "configured": bool(host and sender),
        "host": host,
        "port": int(settings.mail_port),
        "sender": sender,
        "tls": bool(settings.mail_use_tls),
        "ssl": bool(settings.mail_use_ssl),
        "authenticated": bool(settings.mail_username.strip()),
    }


def send_verification_code(*, recipient: str, display_name: str, code: str,
                           purpose: str, expires_minutes: int) -> None:
    if not _valid_mailbox(recipient):
        raise errors.validation("邮箱格式不正确", field="email")
    settings = get_settings()
    _validate_settings(settings)

    purpose_text = {
        "registration": "完成邮箱注册",
        "email_verification": "验证并绑定邮箱",
        "password_reset": "重置登录密码",
    }.get(purpose, "完成账号验证")
    message = EmailMessage()
    sender = settings.mail_from.strip()
    message["From"] = formataddr(("奥皇上品", sender))
    message["To"] = recipient
    if settings.mail_reply_to.strip():
        message["Reply-To"] = settings.mail_reply_to.strip()
    message["Subject"] = "奥皇上品邮箱验证码"
    message.set_content(
        f"您好，{display_name or '用户'}：\n\n"
        f"请使用下面的验证码{purpose_text}：\n\n"
        f"{code}\n\n"
        f"验证码 {expires_minutes} 分钟内有效，只能使用一次。"
        "如果不是您本人操作，请忽略此邮件。\n"
    )

    try:
        _deliver(settings, message)
    except (OSError, smtplib.SMTPException) as exc:
        # Do not write mailbox addresses, SMTP server responses, usernames, or
        # passwords to logs. The exception class is enough for operations.
        logger.warning("account verification email delivery failed: %s",
                       type(exc).__name__)
        raise errors.AppError(
            "MAIL_DELIVERY_FAILED", "验证码邮件发送失败，请稍后重试", status=502) from None


def send_password_reset(*, recipient: str, display_name: str, token: str,
                        expires_minutes: int) -> None:
    """Compatibility wrapper for callers from older local tooling."""
    send_verification_code(recipient=recipient, display_name=display_name,
                           code=token, purpose="password_reset",
                           expires_minutes=expires_minutes)


def verify_connection() -> dict[str, object]:
    """Connect, negotiate configured TLS, and authenticate without sending mail."""
    settings = get_settings()
    _validate_settings(settings)
    try:
        with _smtp_connection(settings) as smtp:
            smtp.ehlo()
            if settings.mail_use_tls:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            _login(smtp, settings)
    except (OSError, smtplib.SMTPException) as exc:
        logger.warning("SMTP connection verification failed: %s", type(exc).__name__)
        raise errors.AppError(
            "MAIL_CONNECTION_FAILED", "SMTP 连接或认证失败，请检查服务端邮件配置", status=502
        ) from None
    result = status()
    result["ok"] = True
    return result


def _validate_settings(settings: object) -> None:
    host = str(getattr(settings, "mail_host", "") or "").strip()
    sender = str(getattr(settings, "mail_from", "") or "").strip()
    reply_to = str(getattr(settings, "mail_reply_to", "") or "").strip()
    username = str(getattr(settings, "mail_username", "") or "").strip()
    password = str(getattr(settings, "mail_password", "") or "")
    if not host or not sender:
        raise errors.AppError(
            "MAIL_NOT_CONFIGURED", "服务器尚未配置邮箱验证码邮件服务", status=503)
    use_tls = bool(getattr(settings, "mail_use_tls", False))
    use_ssl = bool(getattr(settings, "mail_use_ssl", False))
    if use_tls and use_ssl:
        raise errors.AppError(
            "MAIL_CONFIG_INVALID", "邮件服务不能同时启用 TLS 和 SSL", status=500)
    try:
        port = int(getattr(settings, "mail_port", 0))
        timeout = float(getattr(settings, "mail_timeout", 0))
    except (TypeError, ValueError):
        port, timeout = 0, 0
    if (not _valid_mailbox(sender) or (reply_to and not _valid_mailbox(reply_to))
            or any(ch.isspace() for ch in host) or "\x00" in host
            or not 1 <= port <= 65535 or not math.isfinite(timeout)
            or not 0 < timeout <= 300
            or bool(username) != bool(password)
            or (username and not (use_tls or use_ssl))):
        raise errors.AppError(
            "MAIL_CONFIG_INVALID", "邮件服务地址、安全方式或认证配置无效", status=500)


def _valid_mailbox(value: str) -> bool:
    """Accept one mailbox only; reject header fragments and malformed addresses."""
    return is_valid_email_address(value)


def _deliver(settings: object, message: EmailMessage) -> None:
    with _smtp_connection(settings) as smtp:
        if bool(getattr(settings, "mail_use_tls", False)):
            smtp.starttls(context=ssl.create_default_context())
        _login(smtp, settings)
        smtp.send_message(message)


def _smtp_connection(settings: Any):
    host = str(settings.mail_host).strip()
    port = int(settings.mail_port)
    timeout = float(settings.mail_timeout)
    if bool(getattr(settings, "mail_use_ssl", False)):
        return smtplib.SMTP_SSL(
            host, port, timeout=timeout, context=ssl.create_default_context())
    return smtplib.SMTP(host, port, timeout=timeout)


def _login(smtp: smtplib.SMTP, settings: object) -> None:
    username = str(getattr(settings, "mail_username", "") or "").strip()
    password = str(getattr(settings, "mail_password", "") or "")
    if username:
        smtp.login(username, password)
