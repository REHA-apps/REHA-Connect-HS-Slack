from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Schema for the basic health check endpoint."""

    message: str = Field(..., description="A message indicating the service status.")


class LogsResponse(BaseModel):
    """Schema for the debug logs endpoint."""

    logs: list[str] | None = Field(None, description="The list of log lines.")
    error: str | None = Field(
        None, description="Any error encountered while reading logs."
    )
