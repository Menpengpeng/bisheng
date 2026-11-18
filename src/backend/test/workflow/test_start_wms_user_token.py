from types import SimpleNamespace

import pytest

from bisheng.mcp_manage.clients.base import use_wms_user_token
from bisheng.workflow.nodes.start.start import StartNode


@pytest.mark.asyncio
async def test_start_node_injects_wms_user_token_for_current_request():
    node = StartNode.__new__(StartNode)
    node.node_params = {
        "current_time": "2026-11-18 10:00:00",
        "guide_word": "",
        "guide_question": [],
        "preset_question": [],
        "custom_variables": [],
    }
    node._user_info = None
    node.user_id = None
    node.node_data = SimpleNamespace(v=True)

    async with use_wms_user_token("wms-token"):
        result = node._run("run-1")

    assert result["wms_user_token"] == "wms-token"
