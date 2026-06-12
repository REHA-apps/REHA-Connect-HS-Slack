import asyncio  # noqa: D100

from app.db.storage_service import StorageService
from app.domains.crm.hubspot.service import HubSpotService


async def main():
    corr_id = "test-engagements"
    portal_id = "147910822"

    storage = StorageService(corr_id=corr_id)
    hubspot = HubSpotService(corr_id=corr_id, storage=storage)

    # Search for contact Brian Halligan
    contacts = await hubspot.search_contacts(portal_id, "Brian Halligan")
    if not contacts:
        print("Contact not found")
        return

    contact = contacts[0]
    contact_id = str(contact.get("id", ""))
    print(f"Contact ID: {contact_id}")

    client = await hubspot.get_client(portal_id)

    # Try fetching associations
    for entity in ["emails", "meetings", "calls", "tasks", "notes"]:
        try:
            assoc_ids = await client.get_associations("contacts", contact_id, entity)
            print(f"{entity} associated IDs: {assoc_ids}")
            if assoc_ids:
                if entity == "emails":
                    props = ["hs_email_subject", "hs_email_text", "hs_timestamp"]
                elif entity == "meetings":
                    props = [
                        "hs_meeting_title",
                        "hs_meeting_body",
                        "hs_meeting_start_time",
                        "hs_meeting_end_time",
                        "hs_meeting_outcome",
                    ]
                elif entity == "calls":
                    props = [
                        "hs_call_title",
                        "hs_call_body",
                        "hs_call_status",
                        "hs_timestamp",
                    ]
                elif entity == "tasks":
                    props = [
                        "hs_task_subject",
                        "hs_task_body",
                        "hs_task_status",
                        "hs_task_priority",
                        "hs_timestamp",
                    ]
                else:
                    props = ["hs_note_body", "hs_timestamp"]

                details = await client.batch_read(entity, assoc_ids, properties=props)
                for d in details:
                    print(f"  {entity} detail: {d['id']} - {d.get('properties', {})}")
        except Exception as e:
            print(f"Failed to fetch {entity}: {e}")


if __name__ == "__main__":
    asyncio.run(main())
