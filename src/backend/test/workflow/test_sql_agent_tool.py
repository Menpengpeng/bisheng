"""F043 T007 — SqlAgentAPIWrapper tool narrowing and prompt injection."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import create_engine, text

from bisheng_langchain.gpts.load_tools import load_tools
from bisheng_langchain.gpts.tools.sql_agent import tool as mod
from bisheng_langchain.gpts.tools.sql_agent.tool import (
    SqlAgentAPIWrapper,
    _SCHEMA_DISCOVERY_TOOLS,
)


@pytest.fixture()
def sqlite_db(tmp_path):
    db_file = tmp_path / 'agent_test.db'
    engine = create_engine(f'sqlite:///{db_file}')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(64))'))
        conn.execute(text('CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER)'))
    engine.dispose()
    return f'sqlite:///{db_file}'


def _fake_llm():
    # A real BaseChatModel subclass passes the pydantic model_type validator;
    # create_react_agent is monkeypatched in these tests, so bind_tools is
    # never actually called.
    return GenericFakeChatModel(messages=iter([AIMessage(content='ok')]))


@pytest.fixture()
def captured_agent(monkeypatch):
    captured = {}

    def _fake_create_react_agent(llm, tools, prompt, checkpointer=False, **kw):
        captured['tools'] = list(tools)
        captured['prompt'] = prompt
        return MagicMock(name='compiled-agent')

    monkeypatch.setattr(mod, 'create_react_agent', _fake_create_react_agent)
    return captured


def _names(tools):
    return {t.name for t in tools}


def test_default_path_keeps_discovery_tools_and_prompt(sqlite_db, captured_agent):
    wrapper = SqlAgentAPIWrapper(llm=_fake_llm(), sql_address=sqlite_db)
    try:
        names = _names(captured_agent['tools'])
        assert {'sql_db_query', 'sql_db_list_tables', 'sql_db_schema'} <= names
        assert 'sql_db_list_tables' in captured_agent['prompt']
        # No table scoping on the default path
        assert wrapper.selected_tables is None
    finally:
        wrapper.db._engine.dispose()


def test_selected_schema_path_narrows_tools(sqlite_db, captured_agent):
    ddl = 'CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR(64))'
    wrapper = SqlAgentAPIWrapper(
        llm=_fake_llm(),
        sql_address=sqlite_db,
        selected_tables=['users'],
        schema_ddl=ddl,
    )
    try:
        names = _names(captured_agent['tools'])
        assert 'sql_db_query' in names
        assert names.isdisjoint(_SCHEMA_DISCOVERY_TOOLS)
        # scoped SQLDatabase only exposes the selected table
        assert set(wrapper.db.get_usable_table_names()) == {'users'}
    finally:
        wrapper.db._engine.dispose()


def test_selected_schema_path_prompt_carries_ddl_and_dialect(sqlite_db, captured_agent):
    ddl = 'CREATE TABLE users (id INTEGER, note VARCHAR(10) DEFAULT "a{b}c")'
    wrapper = SqlAgentAPIWrapper(
        llm=_fake_llm(),
        sql_address=sqlite_db,
        selected_tables=['users'],
        schema_ddl=ddl,
    )
    try:
        prompt = captured_agent['prompt']
        assert ddl in prompt
        assert wrapper.db.dialect in prompt
        assert 'Do NOT call `sql_db_list_tables`' in prompt
    finally:
        wrapper.db._engine.dispose()


def test_missing_query_tool_fails_loud(sqlite_db, captured_agent, monkeypatch):
    class _Toolkit:
        def __init__(self, db, llm):
            pass

        def get_tools(self):
            only_schema = MagicMock()
            only_schema.name = 'sql_db_schema'
            return [only_schema]

    monkeypatch.setattr(mod, 'SQLDatabaseToolkit', _Toolkit)
    with pytest.raises(RuntimeError, match='sql_db_query'):
        SqlAgentAPIWrapper(
            llm=_fake_llm(),
            sql_address=sqlite_db,
            selected_tables=['users'],
            schema_ddl='CREATE TABLE users (id INTEGER)',
        )


def test_load_tools_passes_optional_params(sqlite_db, monkeypatch):
    captured = {}

    def _fake_get(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    import bisheng_langchain.gpts.load_tools as lt
    monkeypatch.setitem(
        lt._EXTRA_PARAM_TOOLS,
        'sql_agent',
        (_fake_get, ['llm', 'sql_address'], ['selected_tables', 'schema_ddl']),
    )

    llm = _fake_llm()
    load_tools({'sql_agent': {
        'llm': llm,
        'sql_address': sqlite_db,
        'selected_tables': ['users'],
        'schema_ddl': 'DDL TEXT',
    }}, llm=llm)
    assert captured['sql_address'] == sqlite_db
    assert captured['selected_tables'] == ['users']
    assert captured['schema_ddl'] == 'DDL TEXT'


def test_load_tools_without_optional_params_still_works(sqlite_db, monkeypatch):
    captured = {}

    def _fake_get(**kwargs):
        captured.update(kwargs)
        return MagicMock()

    import bisheng_langchain.gpts.load_tools as lt
    monkeypatch.setitem(
        lt._EXTRA_PARAM_TOOLS,
        'sql_agent',
        (_fake_get, ['llm', 'sql_address'], ['selected_tables', 'schema_ddl']),
    )

    llm = _fake_llm()
    load_tools({'sql_agent': {'llm': llm, 'sql_address': sqlite_db}}, llm=llm)
    assert 'selected_tables' not in captured
    assert 'schema_ddl' not in captured
