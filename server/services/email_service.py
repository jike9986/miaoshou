"""受控的 SMTP 邮件发送。

邮件账号只来自运行环境 ``AOHUANG_MAIL_*``，凭据不会进入数据库、日志或响应。
服务默认关闭；未配置时调用方得到稳定错误码，避免误把本地测试当成已经接通。
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

from core import errors
from core.config import get_settings

logger = logging.getLogger(__name__)


def configured() -> bool:
    settings = get_settings()
    return bool(settings.mail_host.strip() and settings.mail_from.strip())


def status() -> dict[str, object]:
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
    }


def send_password_reset(*, recipient: str, display_name: str, token: str,
                        expires_minutes: int) -> None:
    settings = get_settings()
    if not configured():
        raise errors.AppError(
            "MAIL_NOT_CONFIGURED", "服务器尚未配置找回密码邮件服务", status=503)
    if settings.mail_use_tls and settings.mail_use_ssl:
        raise errors.AppError(
            "MAIL_CONFIG_INVALID", "邮件服务不能同时启用 TLS 和 SSL", status=500)

    message = EmailMessage()
    message["From"] = formataddr(("奥皇上品", settings.mail_from.strip()))
    message["To"] = recipient
    if settings.mail_reply_to.strip():
        message["Reply-To"] = settings.mail_reply_to.strip()
    message["Subject"] = "奥皇上品登录密码重置"
    message.set_content(
        f"您好，{display_name or '用户'}：\n\n"
        "我们收到了密码重置请求。请在密码重置页面输入下面的一次性验证码：\n\n"
        f"{token}\n\n"
        f"验证码 {expires_minutes} 分钟内有效，使用一次后立即失效。"
        "如果不是您本人操作，请忽略此邮件。\n"
    )

    try:
        if settings.mail_use_ssl:
            with smtplib.SMTP_SSL(
                    settings.mail_host.strip(), int(settings.mail_port),
                    timeout=float(settings.mail_timeout),
                    context=ssl.create_default_context()) as smtp:
                _login_and_send(smtp, settings, message)
        else:
            with smtplib.SMTP(
                    settings.mail_host.strip(), int(settings.mail_port),
                    timeout=float(settings.mail_timeout)) as smtp:
                if settings.mail_use_tls:
                    smtp.starttls(context=ssl.create_default_context())
                _login_and_send(smtp, settings, message)
    except (OSError, smtplib.SMTPException) as exc:
        # 不写入 host/user/password 或 SMTP 原始响应，防止凭据随异常泄露。
        logger.warning("password reset email delivery failed: %s", type(exc).__name__)
        raise errors.AppError(
            "MAIL_DELIVERY_FAILED", "找回密码邮件发送失败，请稍后重试", status=502) from None


def _login_and_send(smtp: smtplib.SMTP, settings: object,
                    message: EmailMessage) -> None:
    username = str(getattr(settings, "mail_username", "") or "").strip()
    password = str(getattr(settings, "mail_password", "") or "")
    if username:
        smtp.login(username, password)
    smtp.send_message(message)
