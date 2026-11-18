from contextlib import asynccontextmanager

from mcp.client.streamable_http import streamablehttp_client

from bisheng.mcp_manage.clients.base import BaseMcpClient, get_wms_user_token


class StreamableClient(BaseMcpClient):
    """
    SSE client for connecting to the mcp server.
    """

    def __init__(self, url: str, **kwargs):
        """
        Initialize the streamable http client.

        :param url: The URL of the streamable server.
        """
        super().__init__()
        self.url = url
        self.forward_wms_user_token = bool(
            kwargs.pop("forwardWmsUserToken", kwargs.pop("forward_wms_user_token", False))
        )
        self.kwargs = kwargs

    @asynccontextmanager
    async def get_transport(self, is_tool_execution: bool = False):
        """
        Initialize the SSE client.
        """
        kwargs = self.kwargs.copy()
        wms_user_token = get_wms_user_token()
        if self.forward_wms_user_token and wms_user_token:
            headers = dict(kwargs.get("headers") or {})
            headers["Authorization"] = f"Bearer {wms_user_token}"
            kwargs["headers"] = headers
        elif self.forward_wms_user_token and is_tool_execution:
            headers = dict(kwargs.get("headers") or {})
            headers.pop("Authorization", None)
            kwargs["headers"] = headers
        async with streamablehttp_client(url=self.url, **kwargs) as (read, write, _):
            yield read, write

    @asynccontextmanager
    async def get_tool_transport(self):
        async with self.get_transport(is_tool_execution=True) as transport:
            yield transport
