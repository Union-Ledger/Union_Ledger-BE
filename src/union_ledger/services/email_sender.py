from __future__ import annotations

import smtplib
from email.message import EmailMessage

from union_ledger.core.config import Settings, get_settings


class EmailDeliveryError(RuntimeError):
    """Raised when SMTP email delivery fails."""


def _build_verification_email(
    *,
    recipient: str,
    code: str,
    expires_in_seconds: int,
    settings: Settings,
) -> EmailMessage:
    minutes = max(1, expires_in_seconds // 60)

    message = EmailMessage()
    from_name = settings.smtp_from_name.strip()
    from_email = (settings.smtp_from_email or "").strip()
    message["From"] = f"{from_name} <{from_email}>" if from_name else from_email
    message["To"] = recipient
    message["Subject"] = "[Union Ledger] 이메일 인증 코드"
    message.set_content(
        "\n".join(
            [
                "Union Ledger 이메일 인증 코드입니다.",
                "",
                f"인증 코드: {code}",
                f"유효 시간: 약 {minutes}분",
                "",
                "요청하지 않은 경우 이 메일을 무시하세요.",
            ]
        )
    )
    return message


def send_verification_email(recipient: str, code: str, expires_in_seconds: int) -> None:
    settings = get_settings()
    if not settings.smtp_enabled:
        raise EmailDeliveryError("SMTP is disabled. Set SMTP_ENABLED=true to send emails.")

    if not settings.smtp_host:
        raise EmailDeliveryError("SMTP_HOST is required when SMTP is enabled.")
    if not settings.smtp_from_email:
        raise EmailDeliveryError("SMTP_FROM_EMAIL is required when SMTP is enabled.")

    message = _build_verification_email(
        recipient=recipient,
        code=code,
        expires_in_seconds=expires_in_seconds,
        settings=settings,
    )

    try:
        if settings.smtp_use_ssl:
            with smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
            ) as server:
                if settings.smtp_username:
                    server.login(settings.smtp_username, settings.smtp_password or "")
                server.send_message(message)
            return

        with smtplib.SMTP(
            settings.smtp_host,
            settings.smtp_port,
            timeout=settings.smtp_timeout_seconds,
        ) as server:
            server.ehlo()
            if settings.smtp_use_tls:
                server.starttls()
                server.ehlo()
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password or "")
            server.send_message(message)
    except Exception as exc:  # pragma: no cover - network/service dependent
        raise EmailDeliveryError(f"Failed to send verification email: {exc}") from exc