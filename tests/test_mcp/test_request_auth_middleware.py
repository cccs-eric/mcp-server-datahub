import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from datahub_integrations.mcp.request_auth_middleware import (  # type: ignore[import-not-found]
    RequestAuthMiddleware,
    extract_pat_from_authorization_header,
)


class TestExtractPatFromAuthorizationHeader:
    def test_none_returns_none(self):
        assert extract_pat_from_authorization_header(None) is None

    def test_empty_returns_none(self):
        assert extract_pat_from_authorization_header("   ") is None

    def test_bearer_token(self):
        assert (
            extract_pat_from_authorization_header("Bearer my-pat-token")
            == "my-pat-token"
        )

    def test_bearer_token_case_insensitive(self):
        assert extract_pat_from_authorization_header("bEaReR my-pat-token") == "my-pat-token"

    def test_raw_token_supported(self):
        assert extract_pat_from_authorization_header("my-pat-token") == "my-pat-token"

    def test_invalid_scheme_returns_none(self):
        assert extract_pat_from_authorization_header("Basic abc123") is None


class TestRequestAuthMiddleware:
    @pytest.fixture
    def middleware(self):
        return RequestAuthMiddleware(MagicMock())

    @pytest.mark.asyncio
    @patch("datahub_integrations.mcp.request_auth_middleware.get_http_headers")
    async def test_no_authorization_header_uses_default_client(
        self,
        mock_get_http_headers,
        middleware,
    ):
        mock_get_http_headers.return_value = {}
        default_client = MagicMock()
        middleware._base_client = default_client
        context = MagicMock()
        call_next = AsyncMock(return_value={"ok": True})

        with (
            patch.object(middleware, "_get_or_create_client") as mock_get_client,
            patch(
                "datahub_integrations.mcp.request_auth_middleware.with_datahub_client",
                return_value=contextlib.nullcontext(),
            ) as mock_with_client,
        ):
            result = await middleware.on_request(context, call_next)

        assert result == {"ok": True}
        mock_get_client.assert_not_called()
        mock_with_client.assert_called_once_with(default_client)
        call_next.assert_awaited_once_with(context)

    @pytest.mark.asyncio
    @patch("datahub_integrations.mcp.request_auth_middleware.get_http_headers")
    async def test_valid_authorization_header_uses_request_client(
        self,
        mock_get_http_headers,
        middleware,
    ):
        mock_get_http_headers.return_value = {"authorization": "Bearer pat-123"}
        request_client = MagicMock()
        context = MagicMock()
        call_next = AsyncMock(return_value={"ok": True})

        with (
            patch.object(
                middleware,
                "_get_or_create_client",
                return_value=request_client,
            ) as mock_get_client,
            patch(
                "datahub_integrations.mcp.request_auth_middleware.with_datahub_client",
                return_value=contextlib.nullcontext(),
            ) as mock_with_client,
        ):
            result = await middleware.on_request(context, call_next)

        assert result == {"ok": True}
        mock_get_client.assert_called_once_with("pat-123")
        mock_with_client.assert_called_once_with(request_client)
        call_next.assert_awaited_once_with(context)

    @pytest.mark.asyncio
    @patch("datahub_integrations.mcp.request_auth_middleware.get_http_headers")
    async def test_invalid_authorization_scheme_falls_back_to_default(
        self,
        mock_get_http_headers,
        middleware,
    ):
        mock_get_http_headers.return_value = {"authorization": "Basic abc123"}
        default_client = MagicMock()
        middleware._base_client = default_client
        context = MagicMock()
        call_next = AsyncMock(return_value={"ok": True})

        with (
            patch.object(middleware, "_get_or_create_client") as mock_get_client,
            patch(
                "datahub_integrations.mcp.request_auth_middleware.with_datahub_client",
                return_value=contextlib.nullcontext(),
            ) as mock_with_client,
        ):
            result = await middleware.on_request(context, call_next)

        assert result == {"ok": True}
        mock_get_client.assert_not_called()
        mock_with_client.assert_called_once_with(default_client)
        call_next.assert_awaited_once_with(context)

    @patch("datahub_integrations.mcp.request_auth_middleware.DataHubClient")
    def test_build_client_with_token_uses_base_config(
        self,
        mock_datahub_client_cls,
    ):
        base_client = MagicMock()
        middleware = RequestAuthMiddleware(base_client)
        base_config = MagicMock()
        new_config = MagicMock()
        base_config.model_copy.return_value = new_config

        base_client._graph.config = base_config

        request_client = MagicMock()
        mock_datahub_client_cls.return_value = request_client

        result = middleware._build_client_with_token("pat-xyz")

        base_config.model_copy.assert_called_once_with(update={"token": "pat-xyz"})
        mock_datahub_client_cls.assert_called_once_with(config=new_config)
        assert result is request_client

    @pytest.mark.asyncio
    @patch("datahub_integrations.mcp.request_auth_middleware.get_http_headers")
    async def test_header_client_creation_failure_uses_default_client(
        self,
        mock_get_http_headers,
        middleware,
    ):
        mock_get_http_headers.return_value = {"authorization": "Bearer pat-123"}
        default_client = MagicMock()
        middleware._base_client = default_client
        context = MagicMock()
        call_next = AsyncMock(return_value={"ok": True})

        with (
            patch.object(
                middleware,
                "_get_or_create_client",
                side_effect=Exception("boom"),
            ) as mock_get_client,
            patch(
                "datahub_integrations.mcp.request_auth_middleware.with_datahub_client",
                return_value=contextlib.nullcontext(),
            ) as mock_with_client,
        ):
            result = await middleware.on_request(context, call_next)

        assert result == {"ok": True}
        mock_get_client.assert_called_once_with("pat-123")
        mock_with_client.assert_called_once_with(default_client)
        call_next.assert_awaited_once_with(context)

    def test_get_or_create_client_caches_by_token(self, middleware):
        with patch.object(
            middleware,
            "_build_client_with_token",
            side_effect=[MagicMock(name="client1"), MagicMock(name="client2")],
        ) as mock_build:
            c1 = middleware._get_or_create_client("same-token")
            c2 = middleware._get_or_create_client("same-token")
            c3 = middleware._get_or_create_client("other-token")

        assert c1 is c2
        assert c1 is not c3
        assert mock_build.call_count == 2
