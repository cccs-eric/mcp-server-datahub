"""Request-scoped authentication middleware for DataHub MCP server.

Supports extracting a DataHub Personal Access Token (PAT) from the incoming
HTTP Authorization header and applying it to a request-scoped DataHub client.

Behavior:
- If no Authorization header is present, the default client (configured via env)
  is used.
- If Authorization header contains a PAT, a request-scoped client is used for
  that request only.

Security notes:
- PAT values are never logged.
- Only Bearer scheme is accepted when a scheme is provided.
- Header parsing is strict and fails closed (invalid headers are ignored).
"""

import threading
import hashlib
from typing import Optional

import cachetools
import mcp.types as mt
from datahub.sdk.main_client import DataHubClient
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from loguru import logger

from .mcp_server import with_datahub_client

AUTHORIZATION_HEADER = "authorization"

# Keep request-token clients for a short period to avoid repeatedly creating
# identical clients for active HTTP sessions.
AUTH_CLIENT_CACHE_TTL_SECONDS = 300
AUTH_CLIENT_CACHE_MAX_SIZE = 64


def extract_pat_from_authorization_header(
    authorization_header: Optional[str],
) -> Optional[str]:
    """Extract PAT from Authorization header.

    Accepted formats:
    - "Bearer <PAT>" (preferred)
    - "<PAT>" (raw token; accepted for compatibility)

    Returns None for missing/invalid headers.
    """
    if authorization_header is None:
        return None

    value = authorization_header.strip()
    if not value:
        return None

    parts = value.split(None, 1)
    if len(parts) == 1:
        # Raw token fallback (no auth scheme)
        return parts[0]

    scheme, credentials = parts[0], parts[1].strip()
    if scheme.lower() != "bearer" or not credentials:
        return None

    return credentials


class RequestAuthMiddleware(Middleware):
    """Apply request-scoped DataHub auth from Authorization header.

    This middleware runs for each MCP request and, when an Authorization header
    is present, temporarily overrides the DataHub client in context so downstream
    tool logic uses the request PAT.
    """

    def __init__(self, base_client: DataHubClient) -> None:
        self._base_client = base_client
        self._client_cache: cachetools.TTLCache[str, DataHubClient] = cachetools.TTLCache(
            maxsize=AUTH_CLIENT_CACHE_MAX_SIZE,
            ttl=AUTH_CLIENT_CACHE_TTL_SECONDS,
        )
        self._cache_lock = threading.Lock()

    def _build_client_with_token(self, token: str) -> DataHubClient:
        base_config = self._base_client._graph.config

        model_copy = getattr(base_config, "model_copy", None)
        if callable(model_copy):
            # pydantic v2
            config = base_config.model_copy(update={"token": token})
        else:
            # Backward compatibility fallback
            config = base_config.copy(update={"token": token})
        return DataHubClient(config=config)

    @staticmethod
    def _cache_key_for_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _get_or_create_client(self, token: str) -> DataHubClient:
        token_key = self._cache_key_for_token(token)

        with self._cache_lock:
            cached = self._client_cache.get(token_key)
            if cached is not None:
                return cached

        created = self._build_client_with_token(token)

        with self._cache_lock:
            existing = self._client_cache.get(token_key)
            if existing is not None:
                return existing
            self._client_cache[token_key] = created
            return created

    async def on_request(
        self,
        context: MiddlewareContext[mt.Request],
        call_next: CallNext[mt.Request, object],
    ) -> object:
        headers = get_http_headers()
        token = extract_pat_from_authorization_header(
            headers.get(AUTHORIZATION_HEADER)
        )

        request_client = self._base_client

        if token is not None:
            try:
                request_client = self._get_or_create_client(token)
            except Exception:
                logger.warning(
                    "Failed to create request-scoped DataHub client from Authorization header; using default client"
                )

        with with_datahub_client(request_client):
            return await call_next(context)
