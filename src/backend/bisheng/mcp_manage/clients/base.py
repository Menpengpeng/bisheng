import asyncio
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from contextvars import ContextVar
from typing import Any

from mcp import ClientSession

_wms_user_token: ContextVar[str | None] = ContextVar("wms_user_token", default=None)


def _flatten_mcp_error(exc: BaseException) -> str:
    """展开 MCP SDK 包装的异常组，保留实际传输失败原因。"""
    nested = getattr(exc, "exceptions", None)
    if nested:
        messages = [_flatten_mcp_error(item) for item in nested]
        return "; ".join(message for message in messages if message) or str(exc)
    return str(exc).strip() or type(exc).__name__


@asynccontextmanager
async def use_wms_user_token(token: str | None):
    """在单次 Workflow 执行期间保存 WMS 用户令牌。"""
    context_token = _wms_user_token.set(token.strip() if token else None)
    try:
        yield
    finally:
        _wms_user_token.reset(context_token)


def get_wms_user_token() -> str | None:
    """返回当前 Workflow 执行上下文中的 WMS 用户令牌。"""
    return _wms_user_token.get()


class BaseMcpClient(ABC):
    """
    Base class for MCP clients.
    """

    @abstractmethod
    async def get_transport(self):
        raise NotImplementedError("get_mcp_client_transport() must be implemented in subclasses.")

    @asynccontextmanager
    async def get_tool_transport(self):
        """建立用于实际工具执行的 MCP 传输。"""
        async with self.get_transport() as transport:
            yield transport

    @asynccontextmanager
    async def initialize(self):
        """
        Initialize the client.
        """
        async with self.get_transport() as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session

    async def list_tools(self):
        async with self.initialize() as client_session:
            tools = await client_session.list_tools()
        return tools.tools

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """
        Call a tool.
        """
        try:
            async with self.get_tool_transport() as (read, write):
                async with ClientSession(read, write) as client_session:
                    await client_session.initialize()
                    resp = await client_session.call_tool(name, arguments)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
                raise
            return f"Tool call failed: {_flatten_mcp_error(exc)}"
        return resp.model_dump_json()
