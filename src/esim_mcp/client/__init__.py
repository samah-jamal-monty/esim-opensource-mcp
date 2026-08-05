"""Async HTTP client for the eSIM backend."""

from esim_mcp.client.auth import AuthApiClient
from esim_mcp.client.base import (
    ALLOWED_METHODS,
    FORBIDDEN_PATH_MARKERS,
    PERMITTED_MUTATION_ROUTES,
    BackendApiClient,
    BackendOutcome,
    RequestCredentials,
    classify_backend_error,
    enforce_route_is_permitted,
)
from esim_mcp.client.catalog import CatalogApiClient
from esim_mcp.client.purchase import PurchaseApiClient
from esim_mcp.client.wallet import WalletApiClient

__all__ = [
    "ALLOWED_METHODS",
    "FORBIDDEN_PATH_MARKERS",
    "PERMITTED_MUTATION_ROUTES",
    "AuthApiClient",
    "BackendApiClient",
    "BackendOutcome",
    "CatalogApiClient",
    "PurchaseApiClient",
    "RequestCredentials",
    "WalletApiClient",
    "classify_backend_error",
    "enforce_route_is_permitted",
]
