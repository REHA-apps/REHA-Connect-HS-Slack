import asyncio  # noqa: D100
import logging

from app.db.storage_service import StorageService

# Setup basic logging to see progress
logging.basicConfig(level=logging.INFO)


async def migrate_tokens():
    """Migrates all existing plain-text tokens to encrypted format."""
    service = StorageService(corr_id="token-migration")

    print("Fetching all integrations...")
    integrations = await service.list_all_integrations()
    print(f"Found {len(integrations)} integrations.")

    count = 0
    for integ in integrations:
        # When fetched via list_all_integrations, tokens are already decrypted
        # (or returned as-is if plain) by _process_integration.
        access_token = integ.credentials.get("access_token")
        refresh_token = integ.credentials.get("refresh_token")

        if not access_token and not refresh_token:
            print(f"Skipping {integ.workspace_id} {integ.provider} (no tokens).")
            continue

        if access_token:
            print(
                f"Encrypting tokens for workspace={integ.workspace_id} "
                f"provider={integ.provider}..."
            )
            # update_tokens will encrypt the tokens before saving
            await service.update_tokens(
                integ.workspace_id,
                integ.provider,
                str(access_token),
                str(refresh_token) if refresh_token else None,
            )
            count += 1
        else:
            print(
                f"Skipping {integ.workspace_id} {integ.provider} "
                "(missing access_token)."
            )

    print(f"Migration complete. Updated {count} integrations.")


if __name__ == "__main__":
    asyncio.run(migrate_tokens())
