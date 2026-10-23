"""ReAct 输出解析兜底回归测试。

助手应用(react_run -> ainvoke)与工作流 Agent 节点(invoke)共用
get_react_agent_executor, 因此两条业务路径在此一并回归:
1. 标准 JSON blob 的调用/最终答案 —— 原有行为不回归;
2. 模型输出原生 XML 工具调用(线上报错原文) —— 兜底转为 AgentAction, 工具真实执行;
3. 无法解析的纯文本直答 —— 兜底转为最终答案, 不再抛 OutputParserException。
"""
from typing import Any

from langchain_core.agents import AgentFinish
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool
from pydantic import Field

from bisheng_langchain.gpts.agent_types.llm_react_agent import (
    ReActFallbackOutputParser,
    get_react_agent_executor,
)

# 线上报错原文: 模型输出的 Claude 原生 XML 工具调用格式
RAW_XML_TOOL_CALL = """<tool_calls>

<invoke name="knowledge_25">

<parameter name="query" string="true">创建收货单

</parameter>

</invoke></tool_calls>"""

STANDARD_JSON_ACTION = (
    'Thought: 需要检索\n'
    'Action:\n'
    '```json\n'
    '{"action": "knowledge_25", "action_input": {"query": "创建收货单"}}\n'
    '```'
)

FINAL_ANSWER_JSON = (
    'Thought: 检索完成, 可以回答了\n'
    'Action:\n'
    '```json\n'
    '{"action": "Final Answer", "action_input": "已为您创建收货单"}\n'
    '```'
)

PLAIN_TEXT_ANSWER = '好的, 已为您创建收货单, 单号为 SO-20261023-001。'


class RecorderTool(BaseTool):
    name: str = 'knowledge_25'
    description: str = '在知识库中检索'
    calls: list[Any] = Field(default_factory=list, exclude=True)

    def _run(self, query: str, **kwargs: Any) -> str:
        self.calls.append(query)
        return '找到 3 条相关记录'


def _fake_llm(responses: list[str]) -> GenericFakeChatModel:
    return GenericFakeChatModel(messages=iter([AIMessage(content=r) for r in responses]))


# ---------- 解析器单元测试 ----------

def test_parser_keeps_standard_json_action():
    parser = ReActFallbackOutputParser()
    action = parser.parse(STANDARD_JSON_ACTION)
    assert action.tool == 'knowledge_25'
    assert action.tool_input == {'query': '创建收货单'}


def test_parser_keeps_standard_final_answer():
    parser = ReActFallbackOutputParser()
    result = parser.parse(FINAL_ANSWER_JSON)
    assert isinstance(result, AgentFinish)
    assert result.return_values['output'] == '已为您创建收货单'


def test_parser_xml_tool_call_fallback():
    """线上报错原文: 原生 XML 工具调用兜底转为 AgentAction。"""
    parser = ReActFallbackOutputParser()
    action = parser.parse(RAW_XML_TOOL_CALL)
    assert action.tool == 'knowledge_25'
    assert action.tool_input == {'query': '创建收货单'}


def test_parser_plain_text_fallback():
    """纯文本直答兜底转为最终答案, 不再抛异常。"""
    parser = ReActFallbackOutputParser()
    result = parser.parse(PLAIN_TEXT_ANSWER)
    assert isinstance(result, AgentFinish)
    assert result.return_values['output'] == PLAIN_TEXT_ANSWER


# ---------- 端到端: 两条业务路径共用同一执行器 ----------

def test_workflow_path_invoke_with_xml_fallback():
    """工作流 Agent 节点路径: 同步 invoke + chat_history(workflow/nodes/agent 的调用方式)。"""
    tool = RecorderTool()
    llm = _fake_llm([RAW_XML_TOOL_CALL, FINAL_ANSWER_JSON])
    executor = get_react_agent_executor([tool], llm, '你是采购助手', False)
    result = executor.invoke(
        {
            'input': '帮我创建收货单',
            'chat_history': [HumanMessage(content='你好'), AIMessage(content='你好, 有什么可以帮你?')],
        }
    )
    assert tool.calls == ['创建收货单']
    assert result['agent_outcome'].return_values['output'] == '已为您创建收货单'


async def test_assistant_path_ainvoke_with_xml_fallback():
    """助手应用路径: 异步 ainvoke(assistant_agent.react_run 的调用方式)。"""
    tool = RecorderTool()
    llm = _fake_llm([RAW_XML_TOOL_CALL, FINAL_ANSWER_JSON])
    executor = get_react_agent_executor([tool], llm, '你是采购助手', False)
    result = await executor.ainvoke({'input': '帮我创建收货单', 'chat_history': []})
    assert tool.calls == ['创建收货单']
    assert result['agent_outcome'].return_values['output'] == '已为您创建收货单'


async def test_plain_text_answer_does_not_fail_run():
    """模型直接纯文本回答时整轮正常结束, 不再导致会话/节点失败。"""
    tool = RecorderTool()
    llm = _fake_llm([PLAIN_TEXT_ANSWER])
    executor = get_react_agent_executor([tool], llm, '你是采购助手', False)
    result = await executor.ainvoke({'input': '你好', 'chat_history': []})
    assert result['agent_outcome'].return_values['output'] == PLAIN_TEXT_ANSWER
    assert tool.calls == []
