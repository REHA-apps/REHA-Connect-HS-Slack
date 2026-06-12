import asyncio

from app.db.storage_service import StorageService


async def main():
    storage = StorageService()
    # List all workspaces
    workspaces = await storage.workspaces.fetch_many({})
    print("--- WORKSPACES ---")
    for w in workspaces:
        print(
            f"ID: {w.id}, plan: {w.plan}, status: {w.subscription_status}, install_date: {w.install_date}, trial_ends_at: {w.trial_ends_at}, portal_id: {w.portal_id}, slack_team_id: {w.slack_team_id}"
        )

    print("\n--- INTEGRATIONS ---")
    integrations = await storage.integrations.fetch_many({})
    for i in integrations:
        print(
            f"ID: {i.id}, workspace_id: {i.workspace_id}, provider: {i.provider}, metadata: {i.metadata}"
        )


if __name__ == "__main__":
    asyncio.run(main())
