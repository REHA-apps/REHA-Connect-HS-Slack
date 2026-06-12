import asyncio
import os
import sys

# Ensure the root of the project is in the python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.storage_service import StorageService


async def main():
    """Entry point for hard-deleting a workspace."""
    if len(sys.argv) != 2:
        print("Usage: python scripts/hard_delete_workspace.py <workspace_id>")
        sys.exit(1)

    workspace_id = sys.argv[1]

    print(f"Connecting to database to hard-delete workspace: {workspace_id}")
    storage = StorageService()

    try:
        # Atomic cascade delete
        await storage.delete_workspace_cascade(workspace_id)
        print(
            f"✅ Successfully deleted workspace {workspace_id} and all related records."
        )
    except Exception as e:
        print(f"❌ Failed to delete workspace: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
