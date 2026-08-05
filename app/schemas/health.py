from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    """Response body returned by the health endpoint."""

    status: str = Field(default="UP", examples=["UP"])
    service: str = Field(examples=["MCP Service"])
    version: str = Field(examples=["0.1.0"])
