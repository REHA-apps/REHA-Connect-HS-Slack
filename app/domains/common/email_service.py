from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("common.email")


class EmailService:
    """Centralized service for sending transactional and support emails via SMTP.

    Currently supports Gmail/Standard SMTP (via aiosmtplib).
    """

    def __init__(self, corr_id: str | None = None) -> None:
        self.corr_id = corr_id or "system"
        self.sender = settings.SMTP_USERNAME or "support@rehaapps.com"
        self.default_recipient = settings.CONTACT_EMAIL_DESTINATION

    async def send_support_email(
        self,
        subject: str,
        content: str,
        from_email: str | None = None,
        from_name: str | None = None,
        to_email: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> bool:
        """Sends a support request to the specified inbox via SMTP."""
        recipient = to_email or self.default_recipient
        try:
            # 1. Format Body
            meta_str = ""
            if metadata:
                meta_str = "\n".join([f"{k}: {v}" for k, v in metadata.items()])
                meta_str = f"\n\n--- System Context ---\n{meta_str}"

            full_body = (
                f"New Support Request from {from_name or 'Unknown'} ({from_email or 'No Email'})\n"
                f"--------------------------------------------------\n\n"
                f"{content}\n"
                f"{meta_str}"
            )

            # 2. Transport logic: SMTP only
            if settings.SMTP_PASSWORD.get_secret_value():
                await self._send_via_smtp(subject, full_body, recipient)
            else:
                logger.warning("No SMTP transport configured. Logging only.")
                logger.info("Email Content: %s", full_body)

            return True
        except Exception as e:
            logger.exception("Failed to send support email: %s", e)
            return False

    async def _send_via_smtp(self, subject: str, body: str, recipient: str) -> None:
        """Internal helper to send via Gmail SMTP."""
        from email.message import EmailMessage

        import aiosmtplib

        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(body)

        try:
            await aiosmtplib.send(
                message,
                hostname=settings.SMTP_SERVER,
                port=settings.SMTP_PORT,
                username=self.sender,
                password=settings.SMTP_PASSWORD.get_secret_value(),
                use_tls=False,
                start_tls=True,
            )
            logger.info("Email sent via SMTP: %s", subject)
        except Exception as e:
            logger.error("SMTP Send failed: %s", e)
            raise
