from __future__ import annotations  # noqa: D100

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger("api.public.contact")

router = APIRouter(tags=["public"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mask_email(email: str) -> str:
    """Returns a privacy-safe representation of an email address (EEA compliance)."""
    local, _, domain = email.partition("@")
    return f"{local[:2]}***@{domain}"


async def _submit_to_hubspot_inbox(
    name: str, email: str, subject: str, message: str
) -> bool:
    """Creates a support ticket directly in the REHA HubSpot portal."""
    from app.domains.crm.hubspot.service import HubSpotService

    try:
        service = HubSpotService(corr_id="contact-form")
        support_client = await service.get_support_client()

        # 1. Sync Contact to Support Portal
        contact_results = await support_client.search_objects(
            "contacts", query_string=email, properties=["email"]
        )

        if contact_results:
            contact_id = contact_results[0]["id"]
        else:
            name_parts = name.split(" ", 1)
            new_contact = await support_client.create_object(
                "contacts",
                {
                    "email": email,
                    "firstname": name_parts[0],
                    "lastname": name_parts[1] if len(name_parts) > 1 else "",
                    "hs_lead_status": "NEW",
                },
            )
            contact_id = new_contact["id"]

        # 2. Create Support Ticket
        ticket_props = {
            "subject": f"Website Inquiry: {subject}",
            "content": message,
            "hs_pipeline": "0",  # Support Pipeline
            "hs_pipeline_stage": "1",  # New
        }

        # Associate with the contact
        associations = [
            {
                "to": {"id": contact_id},
                "types": [
                    {
                        "associationCategory": "HUBSPOT_DEFINED",
                        "associationTypeId": 16,  # ticket_to_contact
                    }
                ],
            }
        ]

        await support_client.create_object(
            "tickets", ticket_props, associations=associations
        )
        return True
    except Exception as e:
        logger.error("HubSpot Inbox submission failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/contact")
async def contact_form(
    name: Annotated[str, Form()],
    email: Annotated[str, Form()],
    subject: Annotated[str, Form()],
    message: Annotated[str, Form()],
) -> JSONResponse:
    """Handles contact form submissions.

    Forwards the submission payload perfectly to the HubSpot Support Form,
    allowing HubSpot Helpdesk to natively create the Contact, Ticket, and
    send the auto-reply.
    """
    # Log only a masked representation of the email (EEA/GDPR compliance).
    logger.info("Contact form submission from: %s", _mask_email(email))

    try:
        success = await _submit_to_hubspot_inbox(
            name=name,
            email=email,
            subject=subject,
            message=message,
        )
        if not success:
            raise HTTPException(status_code=500, detail="Failed to send email")

        logger.info("Support email sent successfully for %s", _mask_email(email))

        return JSONResponse(
            status_code=200,
            content={
                "success": True,
                "message": "Your message has been sent successfully!",
            },
        )

    except ValueError as e:
        # Missing config — surface as a 503 (infrastructure issue, not user error)
        logger.error("Contact form config error: %s", e)
        raise HTTPException(
            status_code=503,
            detail=(
                "Contact form is temporarily unavailable. "
                "Please email hello@rehaapps.com directly."
            ),
        ) from e
    except Exception as e:
        import traceback

        traceback.print_exc()
        logger.error("Contact form submission failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Failed: {str(e)}",
        ) from e
