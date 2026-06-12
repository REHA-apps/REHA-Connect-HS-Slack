from __future__ import annotations  # noqa: D100

from slack_sdk.models.blocks import (
    DatePickerElement,
    ExternalDataSelectElement,
    InputBlock,
    Option,
    PlainTextInputElement,
    PlainTextObject,
    StaticSelectElement,
)

from app.utils.html import strip_html


class SlackComponentsMixin:
    """Slack-specific UI component builder using official SDK models."""

    def _input(
        self,
        label: str,
        action_id: str,
        placeholder: str = "",
        initial_value: str = "",
        multiline: bool = False,
        optional: bool = False,
    ) -> InputBlock:
        element = PlainTextInputElement(
            action_id=action_id,
            placeholder=PlainTextObject(text=placeholder) if placeholder else None,
            initial_value=initial_value or None,
            multiline=multiline,
        )

        return InputBlock(
            block_id=f"block_{action_id}",
            element=element,
            label=PlainTextObject(text=label),
            optional=optional,
        )

    def _select(
        self,
        label: str,
        action_id: str,
        options: list[tuple[str, str]],
        initial_option: str | None = None,
        optional: bool = False,
    ) -> InputBlock:
        select_options = [
            Option(text=PlainTextObject(text=lbl), value=val) for lbl, val in options
        ]

        # Truncate labels if necessary
        for opt in select_options:
            # opt.text can be PlainTextObject or str depending on SDK version/usage
            # We use getattr/setattr or check type to satisfy pyright
            text_obj = getattr(opt, "text", None)
            if text_obj and hasattr(text_obj, "text"):
                current_text = getattr(text_obj, "text", "")
                if current_text and len(current_text) > 75:  # noqa: PLR2004
                    text_obj.text = current_text[:72] + "..."

        initial = None
        if initial_option:
            initial = next(
                (o for o in select_options if o.value == initial_option), None
            )

        element = StaticSelectElement(
            action_id=action_id,
            options=select_options,
            placeholder=PlainTextObject(text="Select..."),
            initial_option=initial,
        )

        return InputBlock(
            block_id=f"block_{action_id}",
            element=element,
            label=PlainTextObject(text=label),
            optional=optional,
        )

    def _datepicker(
        self,
        label: str,
        action_id: str,
        initial_date: str | None = None,
        optional: bool = False,
    ) -> InputBlock:
        element = DatePickerElement(
            action_id=action_id,
            initial_date=initial_date,
        )

        return InputBlock(
            block_id=f"block_{action_id}",
            element=element,
            label=PlainTextObject(text=label),
            optional=optional,
        )

    def _association_select_input(
        self,
        action_id: str = "association_search",
        label: str = "Associate with Record",
        placeholder: str = "Link to Contact, Deal, or Company...",
    ) -> InputBlock:
        return InputBlock(
            block_id="block_association",
            element=ExternalDataSelectElement(
                action_id=action_id,
                placeholder=PlainTextObject(text=placeholder),
                min_query_length=3,
            ),
            label=PlainTextObject(text=label),
            optional=True,
        )

    def _strip_html(self, text: str) -> str:
        """Remove HTML tags from text."""
        return strip_html(text)
