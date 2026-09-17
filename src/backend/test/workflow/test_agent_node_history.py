from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from bisheng.workflow.nodes.agent.agent import AgentNode
from bisheng.workflow.nodes.agent.db_schema_service import SchemaResult


def test_agent_node_keeps_internal_history_with_tool_messages():
    node = AgentNode.__new__(AgentNode)
    node._chat_history_flag = True
    node._chat_history_num = 10
    node._chat_history_messages = []

    tool_call_message = AIMessage(content='', additional_kwargs={
        'tool_calls': [{
            'id': 'call_1',
            'type': 'function',
            'function': {'name': 'calculator', 'arguments': '{"x":1}'},
        }]
    })
    tool_message = ToolMessage(content='1', tool_call_id='call_1')
    node._append_chat_history_messages([
        HumanMessage(content='hello'),
        tool_call_message,
        tool_message,
        AIMessage(content='done'),
    ])

    messages = node._get_chat_history_messages()

    assert messages[1].additional_kwargs['tool_calls'][0]['function']['name'] == 'calculator'
    assert isinstance(messages[2], ToolMessage)
    assert messages[3].content == 'done'


def test_agent_node_history_respects_window_size():
    node = AgentNode.__new__(AgentNode)
    node._chat_history_flag = True
    node._chat_history_num = 2
    node._chat_history_messages = [
        HumanMessage(content='q1'),
        AIMessage(content='a1'),
        HumanMessage(content='q2'),
        AIMessage(content='a2'),
    ]

    messages = node._get_chat_history_messages()

    assert len(messages) == 2
    assert messages[0].content == 'q2'
    assert messages[1].content == 'a2'


def test_agent_node_parse_log_keeps_only_real_tool_logs():
    node = AgentNode.__new__(AgentNode)
    node.id = 'agent_1'
    node._system_prompt_list = ['system']
    node._user_prompt_list = ['user']
    node._batch_variable_list = []
    node._log_reasoning_content = ['I called search_company with the user query.']
    node._tool_invoke_list = [[
        {
            'type': 'start',
            'run_id': 'run_1',
            'name': 'search_company',
            'input': {'query': 'baidu'},
        },
        {
            'type': 'end',
            'run_id': 'run_1',
            'name': 'search_company',
            'output': 'ok',
        },
    ]]

    logs = node.parse_log('exec_1', {'output': 'final answer'})[0]

    assert [item['key'] for item in logs] == [
        'system_prompt',
        'user_prompt',
        'search_company',
        'agent_1.output',
    ]
    assert len([item for item in logs if item['type'] == 'tool']) == 1
    assert all(item['key'] != 'Thinking about content' for item in logs)


def test_format_schema_cache_log_cache_hit():
    text = AgentNode._format_schema_cache_log(
        SchemaResult(found=['t1', 't2'], ddl='CREATE TABLE t1 ...',
                     fetched_at='2026-09-17T08:00:00+00:00', from_cache=True),
        cache_enabled=True,
    )
    assert '命中' in text
    assert '表数量: 2' in text
    assert '2026-09-17T08:00:00+00:00' in text
    assert '缺失' not in text


def test_format_schema_cache_log_cache_miss_and_disabled():
    miss = AgentNode._format_schema_cache_log(
        SchemaResult(found=['t1'], missing=['t2'], fetched_at='2026-09-17T08:00:00+00:00'),
        cache_enabled=True,
    )
    assert '缓存未命中' in miss
    assert '缺失表: t2' in miss

    disabled = AgentNode._format_schema_cache_log(
        SchemaResult(found=['t1']), cache_enabled=False)
    assert '未启用' in disabled


def test_agent_node_parse_log_includes_schema_cache_summary():
    node = AgentNode.__new__(AgentNode)
    node.id = 'agent_1'
    node._system_prompt_list = ['system']
    node._user_prompt_list = ['user']
    node._batch_variable_list = []
    node._tool_invoke_list = [[]]
    node._sql_schema_log = AgentNode._format_schema_cache_log(
        SchemaResult(found=['t1'], from_cache=True), cache_enabled=True)

    logs = node.parse_log('exec_1', {'output': 'final answer'})[0]

    schema_logs = [item for item in logs if item['key'] == 'sql_schema_cache']
    assert len(schema_logs) == 1
    assert schema_logs[0]['type'] == 'params'
    assert '命中' in schema_logs[0]['value']
    # summary sits with the params entries, before tool logs / output variable
    assert logs[-1]['key'] == 'agent_1.output'
