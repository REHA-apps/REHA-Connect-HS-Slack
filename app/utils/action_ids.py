# app/utils/action_ids.py
"""Centralized registry of Slack Action IDs and Callback IDs.

Used by decorators and the UI builder to ensure consistency across the app.
"""

# Object Viewing
VIEW_OBJECT = "view_object"
SELECT_OBJECT = "select_object"
VIEW_CONTACT_COMPANY = "view_contact_company"
VIEW_CONTACT_DEALS = "view_contact_deals"
VIEW_COMPANY_DEALS = "view_company_deals"
VIEW_DEALS = "view_deals"
VIEW_CONTACTS = "view_contacts"
VIEW_CONTACT_MEETINGS = "view_contact_meetings"
SELECT_OBJECT_TYPE = "select_object_type"
POST_TO_CHANNEL = "post_to_channel"

# Modals & Shortcuts
OPEN_ADD_NOTE_MODAL = "open_add_note_modal"
OPEN_ADD_TASK_MODAL = "open_add_task_modal"
OPEN_UPDATE_DEAL_TYPE_MODAL = "open_update_deal_type_modal"
OPEN_UPDATE_LEAD_SOURCE_MODAL = "open_update_lead_source_modal"
OPEN_UPDATE_FORECAST_AMOUNT_MODAL = "open_update_forecast_amount_modal"
OPEN_AI_RECAP_MODAL = "open_ai_recap_modal"
OPEN_RECORD_RECAP_MODAL = "open_record_recap_modal"
REASSIGN_OWNER = "reassign_owner"
OPEN_CALCULATOR = "open_calculator"
OPEN_SCHEDULE_MEETING_MODAL = "open_schedule_meeting_modal"
OPEN_SUPPORT_TICKET_MODAL = "open_support_ticket_modal"

# Actions
UPDATE_DEAL_STAGE = "update_deal_stage"
LOG_NEXT_STEP = "log_next_step"
LOG_NEXT_STEP_SUBMISSION = "log_next_step_submission"
UPDATE_TASK_STATUS = "update_task_status"
UPDATE_TASK_PRIORITY = "update_task_priority"
TICKET_CLAIM = "ticket_claim"
TICKET_CLOSE = "ticket_close"
TICKET_DELETE = "ticket_delete"
TICKET_TRANSCRIPT = "ticket_transcript"
GATED_FEATURE_CLICK = "gated_feature_click"
OPEN_IN_HUBSPOT = "open_in_hubspot"
UPGRADE_LINK_CLICK = "upgrade_link_click"
CONTACT_SALES_CLICK = "contact_sales_click"

# Submissions
ADD_NOTE_SUBMISSION = "add_note_modal"
ADD_TASK_SUBMISSION = "add_task_modal"
UPDATE_DEAL_TYPE_SUBMISSION = "update_deal_type_modal"
POST_MORTEM_SUBMISSION = "post_mortem_submission"
CALCULATOR_SUBMISSION = "calculator_submission"
NEXT_STEP_ENFORCEMENT_SUBMISSION = "next_step_enforcement_submission"
REASSIGN_OWNER_SUBMISSION = "reassign_owner_submission"
RECORD_RECAP_SUBMISSION = "record_recap_submission_modal"
SCHEDULE_MEETING_SUBMISSION = "schedule_meeting_modal"
SUPPORT_TICKET_SUBMISSION = "support_ticket_submission"

# Uninstallation
CONFIRM_DISCONNECT_HUBSPOT = "confirm_disconnect_hubspot"
EXECUTE_UNIVERSAL_UNINSTALL = "execute_universal_uninstall"
EXECUTE_HUBSPOT_ONLY_UNINSTALL = "execute_hubspot_only_uninstall"
CANCEL_UNINSTALL = "cancel_uninstall"

# Other
ASSOCIATION_SEARCH = "association_search"
