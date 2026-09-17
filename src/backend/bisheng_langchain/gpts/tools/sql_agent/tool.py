
from langchain_community.agent_toolkits import SQLDatabaseToolkit
from langchain_community.utilities import SQLDatabase
from langchain_core.callbacks import CallbackManagerForToolRun
from langchain_core.language_models import BaseLanguageModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import BaseTool
from langgraph.graph.state import CompiledStateGraph as CompiledGraph
from langgraph.prebuilt import create_react_agent
from pydantic import BaseModel, ConfigDict, Field

_agent_system_prompt = """You are an autonomous agent that answers user questions by querying an SQL database through the provided tools.

When a new question arrives, follow the steps *in order*:

1. ALWAYS call `sql_db_list_tables` first.  
   Purpose: discover what tables are available. Never skip this step.

2. Choose the table(s) that are probably relevant, then call `sql_db_schema`
   once for each of those tables to obtain their schemas.

3. Write one syntactically-correct {dialect} SELECT statement.  
   Guidelines for this query:  
   - Return no more than 50 rows **unless** the user explicitly requests another limit.  
   - Select only the columns needed to answer the question; avoid `SELECT *`.  
   - If helpful, add `ORDER BY` on a meaningful column so the most interesting rows appear first.  
   - ABSOLUTELY NO data-modification statements (INSERT, UPDATE, DELETE, DROP, …).  
   - Double-check the SQL before executing.

4. Execute the query with the execution tool `sql_db_query`.  
   If execution fails, inspect the error, revise the SQL, and try again.  
   Repeat until the query runs successfully or you are certain the request
   cannot be satisfied.

5. Read the resulting rows and craft a concise, direct answer for the user.
   If the result set is empty, explain that no matching data was found.

6. Include the final SQL query in your answer unless the user asks you not to.

Remember:  
- List tables → fetch schemas → write & verify SELECT → execute → answer.  
- Never skip steps 1 or 2.  
- Never perform DML.  
- Keep answers focused on the user's question."""


# F043: used when the schema DDL is prefetched (selected tables path). Both the
# dialect and DDL are injected via placeholder replace (not str.format) because
# DDL text may contain brace characters.
_agent_system_prompt_with_schema = """You are an autonomous agent that answers user questions by querying an SQL database through the provided tools.

The complete structure of every table you are allowed to use is already provided below. Do NOT call `sql_db_list_tables` or `sql_db_schema`; they are unavailable and the table list/structure is fully known already.

When a new question arrives:

1. Read the provided table structures and pick the columns needed to answer the question.

2. Write one syntactically-correct __DIALECT_PLACEHOLDER__ SELECT statement against ONLY the provided tables.
   Guidelines:
   - Return no more than 50 rows unless the user explicitly requests another limit.
   - Select only the columns needed; avoid `SELECT *`.
   - Add `ORDER BY` on a meaningful column when it helps.
   - ABSOLUTELY NO data-modification statements (INSERT, UPDATE, DELETE, DROP, …).
   - Double-check the SQL before executing.

3. Execute the query with `sql_db_query`. If it fails, inspect the error, revise
   the SQL, and try again.

4. Read the resulting rows and craft a concise, direct answer. If the result set
   is empty, explain that no matching data was found.

5. Include the final SQL query in your answer unless the user asks you not to.

Available table structures:
__SCHEMA_PLACEHOLDER__"""

# Tools removed on the prefetched-schema path
_SCHEMA_DISCOVERY_TOOLS = {'sql_db_list_tables', 'sql_db_schema'}


class SqlAgentAPIWrapper(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    llm: BaseLanguageModel = Field(description="llm to use for sql agent")
    sql_address: str = Field(description="sql database address for SQLDatabase uri")
    # F043: when selected_tables is set, the usable table scope is narrowed to
    # these tables; schema_ddl carries prefetched DDL injected into the prompt.
    selected_tables: list[str] | None = Field(
        default=None, description="tables scoped for NL2SQL")
    schema_ddl: str | None = Field(
        default=None, description="prefetched schema DDL text")

    db: SQLDatabase | None = None
    agent: CompiledGraph | None = None

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.llm = kwargs.get('llm')
        self.sql_address = kwargs.get('sql_address')
        self.selected_tables = kwargs.get('selected_tables')
        self.schema_ddl = kwargs.get('schema_ddl')

        if self.selected_tables:
            self.db = SQLDatabase.from_uri(
                self.sql_address,
                include_tables=self.selected_tables,
                sample_rows_in_table_info=0,
            )
        else:
            self.db = SQLDatabase.from_uri(self.sql_address)

        toolkit = SQLDatabaseToolkit(db=self.db, llm=self.llm)
        tools = toolkit.get_tools()

        if self.schema_ddl:
            tools = [tool for tool in tools if tool.name not in _SCHEMA_DISCOVERY_TOOLS]
            if not any(tool.name == 'sql_db_query' for tool in tools):
                raise RuntimeError(
                    'sql_db_query tool is unavailable from SQLDatabaseToolkit; '
                    'cannot build the prefetched-schema sql agent'
                )
            prompt = (
                _agent_system_prompt_with_schema
                .replace('__DIALECT_PLACEHOLDER__', self.db.dialect)
                .replace('__SCHEMA_PLACEHOLDER__', self.schema_ddl)
            )
        else:
            prompt = _agent_system_prompt.format(dialect=self.db.dialect)

        self.agent = create_react_agent(
            self.llm,
            tools,
            prompt=prompt,
            checkpointer=False,
        )

    def run(self, query: str) -> str:
        messages = self.agent.invoke({"messages": [HumanMessage(content=query)]})
        return messages["messages"][-1].content

    def arun(self, query: str) -> str:
        return self.run(query)


class SqlAgentInput(BaseModel):
    query: str = Field(description="用户数据查询需求（需要尽可能完整、准确）")


class SqlAgentTool(BaseTool):
    name: str = "sql_agent"
    description: str = "回答与 SQL 数据库有关的问题。给定用户问题，将从数据库中获取可用的表以及对应 DDL，生成 SQL 查询语句并进行执行，最终得到执行结果。"
    args_schema: type[BaseModel] = SqlAgentInput
    api_wrapper: SqlAgentAPIWrapper

    def _run(
            self,
            query: str,
            run_manager: CallbackManagerForToolRun | None = None,
    ) -> str:
        """Use the tool."""
        try:
            res = self.api_wrapper.run(query)
        finally:
            if self.api_wrapper and self.api_wrapper.db:
                self.api_wrapper.db._engine.dispose()
        return res


if __name__ == '__main__':
    from langchain_openai import AzureChatOpenAI

    llm = AzureChatOpenAI()
    sql_agent_tool = SqlAgentTool(
        api_wrapper=SqlAgentAPIWrapper(
            llm=llm,
            sql_address="sqlite:///Chinook.db",
        )
    )

    result = sql_agent_tool.run("Which sales agent made the most in sales in 2009?")
    print(result)
