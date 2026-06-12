from typing import Any, TypedDict  # noqa: D100


class HubSpotProperty(TypedDict):
    value: Any
    timestamp: int | None
    source: str | None
    sourceId: str | None


class HubSpotAssociation(TypedDict):
    id: str
    type: str


class HubSpotAssociationResults(TypedDict):
    results: list[HubSpotAssociation]


class HubSpotObject(TypedDict, total=False):
    id: str
    type: str
    properties: dict[str, Any]
    associations: dict[str, HubSpotAssociationResults] | None
    associated_objects: dict[str, list[Any]] | None
    workspace_id: str | None
    portalId: str | None
    hs_url: str | None
    owner: dict[str, Any] | None


class HubSpotContact(HubSpotObject):
    """HubSpot Contact model."""

    pass


class HubSpotDeal(HubSpotObject):
    """HubSpot Deal model."""

    pass


class HubSpotCompany(HubSpotObject):
    """HubSpot Company model."""

    pass


class HubSpotTicket(HubSpotObject):
    """HubSpot Ticket model."""

    pass


class HubSpotOwner(TypedDict):
    id: str
    email: str | None
    firstName: str | None
    lastName: str | None
    userId: int | None
    teams: list[dict[str, Any]] | None
