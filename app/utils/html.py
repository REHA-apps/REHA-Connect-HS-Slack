import html  # noqa: D100
import re


def strip_html(text: str) -> str:
    """Remove HTML tags from text and normalize newlines."""
    if not text:
        return ""

    # Replace common block elements with newlines for better readability
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"(?i)</div>", "\n", text)
    text = re.sub(r"(?i)</li>", "\n", text)

    # Remove all remaining tags
    clean = re.sub(r"<[^>]+>", "", text)

    # Normalize multiple newlines and strip
    clean = re.sub(r"\n\s*\n", "\n\n", clean)
    return clean.strip()


def sanitize_for_hubspot(text: str) -> str:
    """Sanitize user-provided text before storing in HubSpot HTML fields.

    HubSpot renders hs_note_body and similar fields as HTML. This function
    escapes HTML entities to prevent:
    - Script injection (<script>, onerror=, etc.)
    - Protocol injection (javascript:, data:, vbscript:)
    - Malicious metadata from Slack blocks

    Newlines are preserved as <br> for readability in HubSpot's UI.
    """
    if not text:
        return ""

    # 1. Escape all HTML entities (converts < > & " ' to safe equivalents)
    safe = html.escape(text, quote=True)

    # 2. Strip any residual dangerous protocol patterns that could survive
    #    in attributes if the text is later used in unescaped contexts
    safe = re.sub(r"(?i)(javascript|vbscript|data)\s*:", "blocked:", safe)

    # 3. Convert newlines to <br> for HubSpot HTML rendering
    safe = safe.replace("\n", "<br>")

    return safe
