"""Workflow node model fallback (no database / no real LLM provider needed).

``LLMService.get_bisheng_llm_for_workflow_sync`` must degrade to the tenant
system-config workflow default model when the model configured on the node is
unavailable at configuration level (deleted / provider deleted / wrong type /
offline). Runtime failures are out of scope and keep propagating.

The DB read (``BishengBase.get_model_server_info_sync``) and the system config
read (``LLMService.sync_get_workflow_llm``) are patched so the whole real
validation chain (``BishengLLM._init_client``) still runs.
"""

from unittest.mock import patch

import pytest
from loguru import logger as loguru_logger

from bisheng.common.constants.enums.telemetry import ApplicationTypeEnum
from bisheng.common.errcode.server import LlmModelConfigDeletedError
from bisheng.llm.domain.const import LLMModelType, LLMServerType
from bisheng.llm.domain.llm.base import BishengBase
from bisheng.llm.domain.models import LLMModel, LLMServer
from bisheng.llm.domain.schemas import EvaluationLLMConfig
from bisheng.llm.domain.services import LLMService

# 节点上配置的模型（模拟“已删除 / 已下线”）
CONFIGURED_MODEL_ID = 9101
# 系统模型设置中的工作流默认模型
DEFAULT_MODEL_ID = 9102
SERVER_ID = 9100


def _fake_server() -> LLMServer:
    return LLMServer(
        id=SERVER_ID,
        name="mock-openai",
        type=LLMServerType.OPENAI.value,
        config={"openai_api_key": "sk-mock", "openai_api_base": "http://127.0.0.1:9999/v1"},
        tenant_id=1,
    )


def _fake_model(*, online: bool = True, model_type: str = LLMModelType.LLM.value) -> LLMModel:
    return LLMModel(
        id=DEFAULT_MODEL_ID,
        server_id=SERVER_ID,
        name="Mock Default Model",
        model_name="mock-chat-model",
        model_type=model_type,
        online=online,
        config={},
        tenant_id=1,
    )


def _patch_model_lookup(rows: dict[int, tuple[LLMModel | None, LLMServer | None]]):
    """Patch the DB read used by ``BishengLLM``; unknown ids resolve to None."""

    def _lookup(cls, model_id):
        return rows.get(model_id, (None, None))

    return patch.object(BishengBase, "get_model_server_info_sync", classmethod(_lookup))


def _patch_workflow_default(model_id: int | None):
    return patch.object(
        LLMService,
        "sync_get_workflow_llm",
        classmethod(lambda cls, tenant_id=None: EvaluationLLMConfig(model_id=model_id)),
    )


def _build_workflow_llm(model_id: int):
    return LLMService.get_bisheng_llm_for_workflow_sync(
        model_id=model_id,
        app_id="mock-app",
        app_name="mock-app",
        app_type=ApplicationTypeEnum.WORKFLOW,
        user_id=1,
        tenant_id=1,
    )


def test_deleted_model_falls_back_to_workflow_default():
    """Configured model row is gone -> system workflow default model is used."""
    server = _fake_server()
    default_model = _fake_model()
    logs: list[str] = []
    sink_id = loguru_logger.add(lambda message: logs.append(str(message)), level="WARNING")
    try:
        with (
            _patch_model_lookup({DEFAULT_MODEL_ID: (default_model, server)}),
            _patch_workflow_default(DEFAULT_MODEL_ID),
        ):
            llm = _build_workflow_llm(CONFIGURED_MODEL_ID)
    finally:
        loguru_logger.remove(sink_id)

    assert llm.model_id == DEFAULT_MODEL_ID
    assert llm.model_name == "mock-chat-model"
    # 仅后端日志感知: 告警中同时包含原模型与降级后的默认模型
    assert any(str(CONFIGURED_MODEL_ID) in line and str(DEFAULT_MODEL_ID) in line for line in logs)


def test_offline_model_falls_back_to_workflow_default():
    """Configured model is offline -> system workflow default model is used."""
    server = _fake_server()
    offline_model = _fake_model(online=False)
    default_model = _fake_model()
    with (
        _patch_model_lookup({CONFIGURED_MODEL_ID: (offline_model, server), DEFAULT_MODEL_ID: (default_model, server)}),
        _patch_workflow_default(DEFAULT_MODEL_ID),
    ):
        llm = _build_workflow_llm(CONFIGURED_MODEL_ID)

    assert llm.model_id == DEFAULT_MODEL_ID


def test_wrong_model_type_falls_back_to_workflow_default():
    """Configured id points to a non-chat model -> system workflow default is used."""
    server = _fake_server()
    embedding_model = _fake_model(model_type=LLMModelType.EMBEDDING.value)
    default_model = _fake_model()
    with (
        _patch_model_lookup(
            {CONFIGURED_MODEL_ID: (embedding_model, server), DEFAULT_MODEL_ID: (default_model, server)}
        ),
        _patch_workflow_default(DEFAULT_MODEL_ID),
    ):
        llm = _build_workflow_llm(CONFIGURED_MODEL_ID)

    assert llm.model_id == DEFAULT_MODEL_ID


def test_no_workflow_default_keeps_original_error():
    """No default configured -> the original 'model deleted' error still surfaces."""
    with (
        _patch_model_lookup({}),
        _patch_workflow_default(None),
    ):
        with pytest.raises(LlmModelConfigDeletedError):
            _build_workflow_llm(CONFIGURED_MODEL_ID)


def test_configured_model_is_default_keeps_original_error():
    """Configured model already is the default -> no pointless retry loop."""
    with (
        _patch_model_lookup({}),
        _patch_workflow_default(DEFAULT_MODEL_ID),
    ):
        with pytest.raises(LlmModelConfigDeletedError):
            _build_workflow_llm(DEFAULT_MODEL_ID)


def test_default_model_also_unavailable_propagates_error():
    """Fallback model unavailable too -> error propagates instead of running silently."""
    with (
        _patch_model_lookup({}),
        _patch_workflow_default(DEFAULT_MODEL_ID),
    ):
        with pytest.raises(LlmModelConfigDeletedError):
            _build_workflow_llm(CONFIGURED_MODEL_ID)
