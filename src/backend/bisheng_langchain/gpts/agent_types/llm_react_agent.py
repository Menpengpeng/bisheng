import logging
import operator
import re
from typing import Annotated, Optional, Sequence, TypedDict, Union

from bisheng_langchain.gpts.prompts.react_agent_prompt import react_agent_prompt
from langchain_classic.agents.format_scratchpad import format_log_to_str
from langchain_classic.agents.output_parsers import JSONAgentOutputParser
from langchain_classic.tools import BaseTool
from langchain_classic.tools.render import render_text_description_and_args
from langchain_core.agents import AgentAction, AgentFinish
from langchain_core.exceptions import OutputParserException
from langchain_core.language_models import LanguageModelLike
from langchain_core.messages import BaseMessage
from langchain_core.runnables import RunnablePassthrough
from langgraph.graph import END, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.prebuilt import ToolNode
from langgraph.utils.runnable import RunnableCallable

logger = logging.getLogger(__name__)

# 原生 XML 工具调用格式: <invoke name="xxx">...</invoke>
# (Claude 等模型的内置格式, 外层包裹 <tool_calls> 或 <function_calls>, 包裹层可有可无)
_INVOKE_PATTERN = re.compile(
    r'<invoke\s+name\s*=\s*["\'](?P<tool>[^"\']+)["\']\s*>(?P<body>.*?)</invoke>',
    re.DOTALL,
)
# invoke 块内的参数: <parameter name="xxx" ...>value</parameter>
_PARAMETER_PATTERN = re.compile(
    r'<parameter\s+name\s*=\s*["\'](?P<name>[^"\']+)["\'][^>]*>(?P<value>.*?)</parameter>',
    re.DOTALL,
)


class ReActFallbackOutputParser(JSONAgentOutputParser):
    """带兜底的 ReAct 输出解析器。

    JSONAgentOutputParser 只认 ```json``` blob 格式, 遇到以下两类模型输出会直接抛
    OutputParserException, 导致助手会话(错误码 10499)与工作流 Agent 节点整体失败:
    1. 模型输出了原生 XML 工具调用(如 Claude 的 <tool_calls><invoke .../> 格式);
    2. 模型直接给出不带 "Final Answer" 标记的最终答案。
    解析顺序: 标准 JSON -> 原生 XML 工具调用 -> 原文作为最终答案返回。
    """

    def parse(self, text: str) -> Union[AgentAction, AgentFinish]:
        try:
            return super().parse(text)
        except OutputParserException:
            action = self._parse_xml_tool_call(text)
            if action is not None:
                logger.warning('react output parsed by xml tool-call fallback, tool: %s', action.tool)
                return action
            logger.warning('react output unparseable, fall back to final answer')
            return AgentFinish({'output': text}, text)

    @staticmethod
    def _parse_xml_tool_call(text: str) -> Optional[AgentAction]:
        """从模型输出中解析原生 XML 工具调用, 未识别到 <invoke> 块时返回 None。"""
        invoke_match = _INVOKE_PATTERN.search(text)
        if invoke_match is None:
            return None
        tool_name = invoke_match.group('tool').strip()
        if not tool_name:
            return None
        tool_input = {
            match.group('name').strip(): match.group('value').strip()
            for match in _PARAMETER_PATTERN.finditer(invoke_match.group('body'))
        }
        return AgentAction(tool_name, tool_input, text)


def get_react_agent_executor(tools: list[BaseTool], llm: LanguageModelLike, system_message: str,
                             interrupt_before_action: bool, **kwargs):
    prompt = react_agent_prompt
    prompt = prompt.partial(assistant_message=system_message)
    prompt = prompt.partial(
        tools=render_text_description_and_args(list(tools)),
        tool_names=', '.join([t.name for t in tools]),
    )
    # 与 langchain_classic 的 create_structured_chat_agent 构造等价,
    # 仅将末端解析器替换为带兜底的 ReActFallbackOutputParser
    llm_with_stop = llm.bind(stop=['\nObservation'])
    agent = (
        RunnablePassthrough.assign(
            agent_scratchpad=lambda x: format_log_to_str(x['intermediate_steps']),
        )
        | prompt
        | llm_with_stop
        | ReActFallbackOutputParser()
    )
    agent_executer = create_agent_executor(agent, tools)
    return agent_executer


def _get_agent_state(input_schema=None):
    if input_schema is None:

        class AgentState(TypedDict):
            # The input string
            input: str
            # The list of previous messages in the conversation
            chat_history: Sequence[BaseMessage]
            # The outcome of a given call to the agent
            # Needs `None` as a valid type, since this is what this will start as
            agent_outcome: Union[AgentAction, AgentFinish, None]
            # List of actions and corresponding observations
            # Here we annotate this with `operator.add` to indicate that operations to
            # this state should be ADDED to the existing values (not overwrite it)
            intermediate_steps: Annotated[list[tuple[AgentAction, str]], operator.add]

    else:

        class AgentState(input_schema):
            # The outcome of a given call to the agent
            # Needs `None` as a valid type, since this is what this will start as
            agent_outcome: Union[AgentAction, AgentFinish, None]
            # List of actions and corresponding observations
            # Here we annotate this with `operator.add` to indicate that operations to
            # this state should be ADDED to the existing values (not overwrite it)
            intermediate_steps: Annotated[list[tuple[AgentAction, str]], operator.add]

    return AgentState


def create_agent_executor(agent_runnable, tools, input_schema=None) -> CompiledStateGraph:
    """This is a helper function for creating a graph that works with LangChain Agents.

    Args:
        agent_runnable (RunnableLike): The agent runnable.
        tools (list): A list of tools to be used by the agent.
        input_schema (dict, optional): The input schema for the agent. Defaults to None.

    Returns:
        The `CompiledStateGraph` object.
    """

    tool_executor = {one.name: one for one in tools}

    state = _get_agent_state(input_schema)

    # Define logic that will be used to determine which conditional edge to go down

    def should_continue(data):
        # If the agent outcome is an AgentFinish, then we return `exit` string
        # This will be used when setting up the graph to define the flow
        if isinstance(data['agent_outcome'], AgentFinish):
            return 'end'
        # Otherwise, an AgentAction is returned
        # Here we return `continue` string
        # This will be used when setting up the graph to define the flow
        else:
            return 'continue'

    def run_agent(data, config):
        agent_outcome = agent_runnable.invoke(data, config)
        return {'agent_outcome': agent_outcome}

    async def arun_agent(data, config):
        agent_outcome = await agent_runnable.ainvoke(data, config)
        return {'agent_outcome': agent_outcome}

    # Define the function to execute tools
    def execute_tools(data, config):
        # Get the most recent agent_outcome - this is the key added in the `agent` above
        agent_action = data['agent_outcome']
        if not isinstance(agent_action, list):
            agent_action = [agent_action]

        output = []
        for action in agent_action:
            if action.tool not in tool_executor:
                output.append(f"Tool {action.tool} not found in tool executor.")
                continue
            try:
                result = tool_executor[action.tool].invoke(action.tool_input, config)
                output.append(result)
            except Exception as e:
                output.append(f"invoke tool {action.tool} failed: {str(e)[-50:]}")

        return {
            'intermediate_steps': [(action, str(out)) for action, out in zip(agent_action, output)]
        }

    async def aexecute_tools(data, config):
        # Get the most recent agent_outcome - this is the key added in the `agent` above
        agent_action = data['agent_outcome']
        if not isinstance(agent_action, list):
            agent_action = [agent_action]
        output = []
        for action in agent_action:
            if action.tool not in tool_executor:
                output.append(f"Tool {action.tool} not found in tool executor.")
                continue
            try:
                result = await tool_executor[action.tool].ainvoke(action.tool_input, config)
                output.append(result)
            except Exception as e:
                output.append(f"invoke tool {action.tool} failed: {str(e)[-50:]}")

        return {
            'intermediate_steps': [(action, str(out)) for action, out in zip(agent_action, output)]
        }

    # Define a new graph
    workflow = StateGraph(state)

    # Define the two nodes we will cycle between
    workflow.add_node('agent', RunnableCallable(run_agent, arun_agent))
    workflow.add_node('tools', RunnableCallable(execute_tools, aexecute_tools))

    # Set the entrypoint as `agent`
    # This means that this node is the first one called
    workflow.set_entry_point('agent')

    # We now add a conditional edge
    workflow.add_conditional_edges(
        # First, we define the start node. We use `agent`.
        # This means these are the edges taken after the `agent` node is called.
        'agent',
        # Next, we pass in the function that will determine which node is called next.
        should_continue,
        # Finally we pass in a mapping.
        # The keys are strings, and the values are other nodes.
        # END is a special node marking that the graph should finish.
        # What will happen is we will call `should_continue`, and then the output of that
        # will be matched against the keys in this mapping.
        # Based on which one it matches, that node will then be called.
        {
            # If `tools`, then we call the tool node.
            'continue': 'tools',
            # Otherwise we finish.
            'end': END,
        },
    )

    # We now add a normal edge from `tools` to `agent`.
    # This means that after `tools` is called, `agent` node is called next.
    workflow.add_edge('tools', 'agent')

    # Finally, we compile it!
    # This compiles it into a LangChain Runnable,
    # meaning you can use it as you would any other runnable
    return workflow.compile(checkpointer=False)


if __name__ == '__main__':
    pass
