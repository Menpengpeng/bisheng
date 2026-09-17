import json
from typing import Annotated, Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.retrievers import BaseRetriever
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import ArgsSchema, BaseTool
from langgraph.prebuilt import create_react_agent
from loguru import logger
from pydantic import BaseModel, Field, SkipValidation, field_validator

from bisheng.citation.domain.schemas.citation_schema import CitationRegistryItemSchema
from bisheng.citation.domain.services.citation_prompt_helper import (
    CITATION_PROMPT_RULES,
    annotate_rag_documents_with_citations,
    annotate_web_results_with_citations,
    cache_citation_registry_items,
    cache_citation_registry_items_sync,
    collect_rag_citation_registry_items,
    collect_web_citation_registry_items,
    prompt_has_citation_rules,
)
from bisheng.common.constants.enums.telemetry import ApplicationTypeEnum
from bisheng.knowledge.domain.knowledge_rag import KnowledgeRag
from bisheng.knowledge.domain.models.knowledge import KnowledgeDao
from bisheng.knowledge.domain.services.knowledge_utils import KnowledgeUtils
from bisheng.llm.domain.services import LLMService
from bisheng.tool.domain.services.executor import ToolExecutor
from bisheng.workflow.callback.event import StreamMsgOverData
from bisheng.workflow.callback.llm_callback import LLMNodeCallbackHandler
from bisheng.workflow.nodes.base import BaseNode
from bisheng.workflow.nodes.prompt_template import PromptTemplateParser
from bisheng_langchain.agents.llm_functions_agent.base import _format_intermediate_steps
from bisheng_langchain.gpts.assistant import ConfigurableAssistant
from bisheng_langchain.gpts.load_tools import load_tools

agent_executor_dict = {
    "ReAct": "get_react_agent_executor",
    "function call": "get_openai_functions_agent_executor",
}


class WorkflowCitationToolWrapper(BaseTool):
    """Add citation prompt context for workflow agent tool invocations."""

    name: str
    description: str
    args_schema: Annotated[ArgsSchema | None, SkipValidation()] = Field(default=None)
    tool: BaseTool
    citation_registry_items: list[CitationRegistryItemSchema] = Field(default_factory=list, exclude=True)
    kb_name_by_id: dict[str, str] = Field(default_factory=dict, exclude=True)

    @classmethod
    def wrap(cls, tool: BaseTool) -> BaseTool:
        return cls(
            name=tool.name,
            description=tool.description,
            args_schema=tool.args_schema,
            tool=tool,
        )

    def _is_web_search_tool(self) -> bool:
        return self.tool.name == "web_search" or getattr(self.tool, "tool_name", None) == "web_search"

    def _has_knowledge_rag_tool(self) -> bool:
        return hasattr(self.tool, "knowledge_retriever_tool")

    def _append_web_citation(self, output: Any) -> Any:
        if not isinstance(output, str):
            return output
        try:
            results = json.loads(output)
        except json.JSONDecodeError:
            return output
        if not isinstance(results, list):
            return output
        results = annotate_web_results_with_citations(results)
        self._extend_citation_registry_items(collect_web_citation_registry_items(results))
        return json.dumps(results, ensure_ascii=False)

    async def _aappend_web_citation(self, output: Any) -> Any:
        if not isinstance(output, str):
            return output
        try:
            results = json.loads(output)
        except json.JSONDecodeError:
            return output
        if not isinstance(results, list):
            return output
        results = annotate_web_results_with_citations(results)
        await self._aextend_citation_registry_items(collect_web_citation_registry_items(results))
        return json.dumps(results, ensure_ascii=False)

    def _format_knowledge_results(self, retrieval_result: Any) -> str:
        source_documents = list(retrieval_result or [])
        source_documents = annotate_rag_documents_with_citations(source_documents)
        self._extend_citation_registry_items(collect_rag_citation_registry_items(source_documents))
        return self._dump_knowledge_chunks(source_documents)

    async def _aformat_knowledge_results(self, retrieval_result: Any) -> str:
        source_documents = list(retrieval_result or [])
        source_documents = annotate_rag_documents_with_citations(source_documents)
        await self._aextend_citation_registry_items(collect_rag_citation_registry_items(source_documents))
        return self._dump_knowledge_chunks(source_documents)

    def _dump_knowledge_chunks(self, source_documents: list) -> str:
        """Serialise retrieved chunks into the tool-output format aligned with the
        workstation/RAG retrieved_result (no inner LLM call — the agent's main model
        does the synthesis and emits citations from the per-chunk citation_key)."""
        results = []
        for doc in source_documents:
            meta = getattr(doc, "metadata", {}) or {}
            kb_id_raw = meta.get("knowledge_id") or meta.get("kb_id") or ""
            kb_id = str(kb_id_raw) if kb_id_raw not in (None, "") else ""
            kb_name = self.kb_name_by_id.get(kb_id, "")
            results.append(KnowledgeUtils.format_retrieved_chunk(doc, kb_name))
        return json.dumps(results, ensure_ascii=False)

    def _extend_citation_registry_items(self, items: list[CitationRegistryItemSchema]) -> None:
        cache_citation_registry_items_sync(items)
        self.citation_registry_items.extend(items)

    async def _aextend_citation_registry_items(self, items: list[CitationRegistryItemSchema]) -> None:
        await cache_citation_registry_items(items)
        self.citation_registry_items.extend(items)

    def _run(self, query: str, config: RunnableConfig = None, **kwargs: Any) -> Any:
        if self._is_web_search_tool():
            return self._append_web_citation(self.tool.invoke({"query": query}, config=config))
        if not self._has_knowledge_rag_tool():
            return self.tool.invoke({"query": query}, config=config)

        retrieval_result = self.tool.knowledge_retriever_tool.invoke({"query": query}, config=config)
        return self._format_knowledge_results(retrieval_result)

    async def _arun(self, query: str, config: RunnableConfig = None, **kwargs: Any) -> Any:
        if self._is_web_search_tool():
            return await self._aappend_web_citation(await self.tool.ainvoke({"query": query}, config=config))
        if not self._has_knowledge_rag_tool():
            return await self.tool.ainvoke({"query": query}, config=config)

        retrieval_result = await self.tool.knowledge_retriever_tool.ainvoke({"query": query}, config=config)
        return await self._aformat_knowledge_results(retrieval_result)


class SqlAgentParams(BaseModel):
    """SQL Agent Param Model"""

    database_engine: str | None = Field(
        "mysql", description="Database type, support mysql, db2, postgres, gaussdb, oracle, sqlserver, dm"
    )
    db_username: str
    db_password: str
    db_address: str
    db_name: str
    open: bool = False
    # F043: tables selected on the canvas; empty means the legacy self-discovery path
    selected_tables: list[str] = Field(default_factory=list, description="Selected tables for NL2SQL")
    # F043: when enabled, prefetched schema DDL is cached in Redis
    schema_cache_enabled: bool = Field(False, description="Enable schema cache")
    # F043: user-configured cache lifetime in hours; default 24h (clamped 1-720)
    schema_cache_ttl: int = Field(24, description="Schema cache TTL in hours")

    @field_validator("schema_cache_ttl")
    @classmethod
    def validate_schema_cache_ttl(cls, v):
        # Clamp instead of raising: a bad saved value must never block loading
        # the workflow; the UI also enforces the 1-720 hours range.
        if v is None:
            return 24
        try:
            v = int(v)
        except (TypeError, ValueError):
            return 24
        return max(1, min(720, v))

    @field_validator("database_engine")
    @classmethod
    def validate_database_engine(cls, v):
        # Convert to lowercase
        if v:
            v = v.lower()
            if v not in ["mysql", "db2", "postgres", "gaussdb", "oracle", "postgresql", "sqlserver", "dm", "dm8"]:
                raise ValueError(
                    "Unsupported database engine. "
                    "Supported engines are: MySQL, DB2, PostgreSql, GaussDB, Oracle, SQLServer, DM."
                )
        return v


class AgentNode(BaseNode):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Determine if it is a single or batch
        self._tab = self.node_data.tab["value"]

        # analyzingprompt
        self._system_prompt = PromptTemplateParser(template=self.node_params["system_prompt"])
        self._system_variables = self._system_prompt.extract()
        self._user_prompt = PromptTemplateParser(template=self.node_params["user_prompt"])
        self._user_variables = self._user_prompt.extract()

        self._image_prompt = self.node_params.get("image_prompt", [])

        self._batch_variable_list = []
        self._system_prompt_list = []
        self._user_prompt_list = []
        self._tool_invoke_list = []
        self._log_reasoning_content = []
        self._chat_history_messages: list[BaseMessage] = []

        # Chat Message
        self._chat_history_flag = self.node_params["chat_history_flag"]["value"] > 0
        self._chat_history_num = self.node_params["chat_history_flag"]["value"]

        self._llm = LLMService.get_bisheng_llm_sync(
            model_id=self.node_params["model_id"],
            temperature=self.node_params.get("temperature", 1),
            app_id=self.workflow_id,
            app_name=self.workflow_name,
            app_type=ApplicationTypeEnum.WORKFLOW,
            user_id=self.user_id,
        )

        # Whether to output the results to the user
        self._output_user = self.node_params.get("output_user", False)

        # tools
        self._tools = self.node_params["tool_list"]

        # knowledge
        # self._knowledge_ids = self.node_params['knowledge_id']
        # Determine whether it is a knowledge base or a temporary file list
        self._knowledge_type = self.node_params["knowledge_id"]["type"]
        self._knowledge_ids = [one["key"] for one in self.node_params["knowledge_id"]["value"]]
        # F041: 用户知识库权限校验 toggle (default OFF, preserves current behavior).
        # ON  → filter knowledge-space retrieval by the runtime user's view_file;
        # OFF → by the config author's (flow creator) view_file.
        self._knowledge_auth = self.node_params.get("user_auth", False)

        # Supported or notnl2sql
        self._sql_agent_params = self.node_params.get("sql_agent", None)
        self._sql_agent = (
            SqlAgentParams.model_validate(self.node_params["sql_agent"])
            if (self._sql_agent_params and self._sql_agent_params.get("open", False))
            else None
        )
        self._sql_address = ""
        # F043: schema source summary (cache hit / db fetch), shown in the
        # run log panel via parse_log -> on_node_end. None when SQL is unused.
        self._sql_schema_log: str | None = None
        if self._sql_agent and self._sql_agent.open:
            self._sql_address = self._init_sql_address()

        # agent
        self._agent_executor_type = "React"
        self._agent = None
        self._citation_tools: list[WorkflowCitationToolWrapper] = []

    def _get_chat_history_messages(self) -> list[BaseMessage]:
        if not self._chat_history_flag:
            return []
        if not self._chat_history_num:
            return list(self._chat_history_messages)
        return self._chat_history_messages[-self._chat_history_num :]

    def _append_chat_history_messages(self, messages: list[BaseMessage]) -> None:
        if not messages:
            return
        self._chat_history_messages.extend(messages)

    def _init_agent(self, system_prompt: str):
        # Get a list of configured helper models
        assistant_llm = LLMService.sync_get_assistant_llm(tenant_id=self.tenant_id)
        if not assistant_llm.llm_list:
            raise Exception("Assistant reasoning model list is empty")
        default_llm = [one for one in assistant_llm.llm_list if one.model_id == self.node_params["model_id"]]
        if not default_llm:
            raise Exception("The selected inference model is not in the list of assistant inference models")
        default_llm = default_llm[0]
        self._agent_executor_type = default_llm.agent_executor_type
        knowledge_retriever = {
            "max_content": default_llm.knowledge_max_content,
            "sort_by_source_and_index": default_llm.knowledge_sort_index,
        }

        func_tools = self._init_tools()
        knowledge_tools = self._init_knowledge_tools(knowledge_retriever)
        sql_agent_tools = self.init_sql_agent_tool()
        func_tools.extend(knowledge_tools)
        func_tools.extend(sql_agent_tools)
        func_tools = self._wrap_citation_tools(func_tools)
        kb_name_by_id = self._resolve_kb_name_by_id()
        for tool in func_tools:
            if isinstance(tool, WorkflowCitationToolWrapper) and tool._has_knowledge_rag_tool():
                tool.kb_name_by_id = kb_name_by_id
        self._citation_tools = [tool for tool in func_tools if isinstance(tool, WorkflowCitationToolWrapper)]
        # Citation-rule backstop: only inject when the node has citation tools and its own
        # system prompt doesn't already carry the rules (the default template does), so
        # existing nodes keep citations and updated prompts aren't duplicated.
        if self._agent_executor_type == "ReAct":
            self._agent = ConfigurableAssistant(
                agent_executor_type=agent_executor_dict.get(self._agent_executor_type),
                tools=func_tools,
                llm=self._llm,
                assistant_message=system_prompt,
            )
        else:
            self._agent = create_react_agent(self._llm, func_tools, prompt=system_prompt, checkpointer=False)

    def _resolve_kb_name_by_id(self) -> dict[str, str]:
        """Map knowledge_id -> name for the agent's knowledge bases, used to fill
        <knowledge_base_name> in retrieved-chunk output. Empty for temp-file type."""
        if self._knowledge_type != "knowledge" or not self._knowledge_ids:
            return {}
        kb_ids = []
        for one in self._knowledge_ids:
            try:
                kb_ids.append(int(one))
            except (TypeError, ValueError):
                continue
        if not kb_ids:
            return {}
        return {str(kb.id): (kb.name or "") for kb in KnowledgeDao.get_list_by_ids(kb_ids)}

    @classmethod
    def _wrap_citation_tools(cls, tools: list[BaseTool]) -> list[BaseTool]:
        return [cls._wrap_citation_tool(tool) for tool in tools]

    @staticmethod
    def _wrap_citation_tool(tool: BaseTool) -> BaseTool:
        if tool.name == "web_search" or hasattr(tool, "knowledge_retriever_tool"):
            return WorkflowCitationToolWrapper.wrap(tool)
        return tool

    @staticmethod
    def _has_citation_tools(tools: list[BaseTool]) -> bool:
        return any(isinstance(tool, WorkflowCitationToolWrapper) for tool in tools)

    def _init_tools(self):
        if self._tools:
            tool_ids = [int(one["key"]) for one in self._tools]
            return ToolExecutor.init_by_tool_ids_sync(
                tool_ids,
                app_id=self.workflow_id,
                app_name=self.workflow_name,
                app_type=ApplicationTypeEnum.WORKFLOW,
                user_id=self.user_id,
            )
        else:
            return []

    def init_sql_agent_tool(self):
        if not self._sql_address:
            return []
        sql_tool_config = {"llm": self._llm, "sql_address": self._sql_address}

        # F043: when tables are preselected, fetch their schema DDL up front
        # (Redis cached when enabled) and narrow the SQL agent to them. An
        # empty selection keeps the legacy list/schema self-discovery path.
        selected_tables = self._sql_agent.selected_tables or []
        if selected_tables:
            from bisheng.workflow.nodes.agent.db_schema_service import get_schema_with_cache

            schema_result = get_schema_with_cache(self._sql_agent, self.tenant_id)
            sql_tool_config["selected_tables"] = schema_result.found
            if schema_result.ddl:
                sql_tool_config["schema_ddl"] = schema_result.ddl
            # 运行日志面板展示 Schema 来源(缓存命中/回源), 经 parse_log 推送前端
            self._sql_schema_log = self._format_schema_cache_log(
                schema_result, self._sql_agent.schema_cache_enabled)
            logger.info(
                "act=sql_agent_schema_prepared tables={} missing={} from_cache={}",
                len(schema_result.found), len(schema_result.missing), schema_result.from_cache,
            )
        else:
            # 未选表: 走运行时自发现路径, 不使用缓存, 也在运行日志中说明
            self._sql_schema_log = "未选择数据表, Agent 将在运行时自行获取表结构(不使用 Schema 缓存)"

        tool_params = {"sql_agent": sql_tool_config}
        return load_tools(tool_params=tool_params, llm=self._llm)

    @staticmethod
    def _format_schema_cache_log(schema_result, cache_enabled: bool) -> str:
        """Summarize the schema source for the run log panel."""
        if schema_result.from_cache:
            source = "Schema 缓存(命中)"
        elif cache_enabled:
            source = "数据库实时拉取(缓存未命中, 已写回缓存)"
        else:
            source = "数据库实时拉取(缓存未启用)"
        lines = [f"Schema 来源: {source}", f"表数量: {len(schema_result.found)}"]
        if schema_result.missing:
            lines.append(f"缺失表: {', '.join(schema_result.missing)}")
        if schema_result.fetched_at:
            lines.append(f"拉取时间: {schema_result.fetched_at}")
        return "\n".join(lines)

    def _init_knowledge_tools(self, knowledge_retriever: dict):
        if not self._knowledge_ids:
            return []
        if self._knowledge_type == "space":
            # F041: one tool covering all selected knowledge spaces, retrieving
            # through the F029 view_file filter (identity = runtime user when the
            # permission toggle is ON, config author when OFF).
            from bisheng.knowledge.domain.services.space_flow_retrieval import build_space_knowledge_tool

            identity_user_id = self.user_id if self._knowledge_auth else self.flow_user_id
            space_tool = build_space_knowledge_tool(
                name="knowledge_space_retriever",
                description="在知识空间中检索与查询相关的文档内容。",
                llm=self._llm,
                space_ids=self._knowledge_ids,
                identity_user_id=identity_user_id,
                tenant_id=self.tenant_id,
                max_content=knowledge_retriever.get("max_content", 15000),
                access_scope="per_user" if self._knowledge_auth else "shared",
            )
            return [space_tool]
        tools = []
        for index, knowledge_id in enumerate(self._knowledge_ids):
            if self._knowledge_type == "knowledge":
                knowledge_tool = ToolExecutor.init_knowledge_tool_sync(
                    self.user_id, knowledge_id, llm=self._llm, **knowledge_retriever
                )
                tools.append(knowledge_tool)
            else:
                file_metadata_list = self.get_other_node_variable(knowledge_id)
                if not file_metadata_list:
                    # Do not retrieve if no file has been uploaded
                    continue
                description = ""
                for one in file_metadata_list:
                    description += f"<{one.get('document_name')}>:<{one.get('abstract')}>; "
                tool_init_params = {
                    "name": f"{knowledge_id.split('.')[-1].replace('#', '')}_knowledge_{index}",
                    "description": description,
                    "vector_retriever": self.init_file_milvus(file_metadata_list[0]),
                    "elastic_retriever": self.init_file_es(file_metadata_list[0]),
                    "llm": self._llm,
                    **knowledge_retriever,
                }
                tmp_file_tool = ToolExecutor.init_tmp_knowledge_tool_sync(**tool_init_params)
                tools.append(tmp_file_tool)
        return tools

    def init_file_milvus(self, file_metadata: dict) -> BaseRetriever:
        """Initialize the temporary file selected by the usermilvus"""
        embeddings = LLMService.get_knowledge_default_embedding(self.user_id, tenant_id=self.tenant_id)
        if not embeddings:
            raise Exception("No default configuredembeddingModels")
        file_ids = [file_metadata["document_id"]]
        collection_name = self.get_milvus_collection_name(embeddings.model_id)
        vector_client = KnowledgeRag.init_milvus_vectorstore(collection_name=collection_name, embeddings=embeddings)
        return vector_client.as_retriever(search_kwargs={"expr": f"document_id in {file_ids}"})

    def init_file_es(self, file_metadata: dict):
        es_client = KnowledgeRag.init_es_vectorstore_sync(index_name=self.tmp_collection_name)
        return es_client.as_retriever(
            search_kwargs={"filter": [{"term": {"metadata.document_id": file_metadata["document_id"]}}]}
        )

    def _init_sql_address(self) -> str:
        """Initialize SQL Database Address (delegated to db_schema_service)."""
        if not self._sql_agent:
            return ""
        from bisheng.workflow.nodes.agent.db_schema_service import build_sql_uri

        return build_sql_uri(self._sql_agent).uri

    def _run(self, unique_id: str):
        ret = {}
        variable_map = {}

        self._batch_variable_list = []
        self._system_prompt_list = []
        self._user_prompt_list = []
        self._tool_invoke_list = []
        self._log_reasoning_content = []

        for one in self._system_variables:
            variable_map[one] = self.get_other_node_variable(one)
        system_prompt = self._system_prompt.format(variable_map)
        self._system_prompt_list.append(system_prompt)
        self._init_agent(system_prompt)

        if self._tab == "single":
            self._tool_invoke_list.append([])
            ret["output"], reasoning_content, citation_items = self._run_once(
                None,
                unique_id,
                "output",
                self._tool_invoke_list[0],
            )
            self._log_reasoning_content.append(reasoning_content)
            if self._output_user:
                self.callback_manager.on_stream_over(
                    StreamMsgOverData(
                        node_id=self.id,
                        name=self.name,
                        msg=ret["output"],
                        reasoning_content=reasoning_content,
                        unique_id=unique_id,
                        output_key="output",
                        citation_registry_items=citation_items,
                    )
                )
        else:
            for index, one in enumerate(self.node_params["batch_variable"]):
                self._batch_variable_list.append(self.get_other_node_variable(one))
                output_key = self.node_params["output"][index]["key"]
                self._tool_invoke_list.append([])
                ret[output_key], reasoning_content, citation_items = self._run_once(
                    one,
                    unique_id,
                    output_key,
                    self._tool_invoke_list[index],
                )
                self._log_reasoning_content.append(reasoning_content)
                if self._output_user:
                    self.callback_manager.on_stream_over(
                        StreamMsgOverData(
                            node_id=self.id,
                            name=self.name,
                            msg=ret[output_key],
                            reasoning_content=reasoning_content,
                            unique_id=unique_id,
                            output_key=output_key,
                            citation_registry_items=citation_items,
                        )
                    )

        logger.debug("agent_over result={}", ret)
        if self._output_user:
            # Nonstream Mode, processing results
            for k, v in ret.items():
                answer = v
                self.graph_state.save_context(content=answer, msg_sender="AI")

        return ret

    def parse_log(self, unique_id: str, result: dict) -> Any:
        ret = []
        index = 0
        for k, v in result.items():
            one_ret = [
                {"key": "system_prompt", "value": self._system_prompt_list[0], "type": "params"},
                {"key": "user_prompt", "value": self._user_prompt_list[index], "type": "params"},
            ]
            if self._batch_variable_list:
                one_ret.insert(
                    0, {"key": "batch_variable", "value": self._batch_variable_list[index], "type": "variable"}
                )

            # F043: schema cache source summary (sql agent only)
            sql_schema_log = getattr(self, "_sql_schema_log", None)
            if sql_schema_log:
                one_ret.append(
                    {"key": "sql_schema_cache", "value": sql_schema_log, "type": "params"}
                )

            # Handler Call Log
            one_ret.extend(self.parse_tool_log(self._tool_invoke_list[index]))
            one_ret.append({"key": f"{self.id}.{k}", "value": v, "type": "variable"})
            ret.append(one_ret)
            index += 1
        return ret

    def parse_tool_log(self, tool_invoke_list: list) -> list:
        ret = []
        tool_invoke_info = {}
        for one in tool_invoke_list:
            if one["run_id"] not in tool_invoke_info:
                tool_invoke_info[one["run_id"]] = {}
            if one["type"] == "start":
                tool_invoke_info[one["run_id"]].update({"name": one["name"], "input": one["input"]})
            elif one["type"] == "end":
                tool_invoke_info[one["run_id"]].update({"output": one["output"]})
            elif one["type"] == "error":
                tool_invoke_info[one["run_id"]].update({"output": f"Error: {one['error']}"})
        if tool_invoke_info:
            tool_logs = list(tool_invoke_info.values())
            tool_logs = self._dedupe_web_search_tool_logs(tool_logs)
            for one in tool_logs:
                # knowledge_retriever_tool belong into rag logic，not show in tool log
                if one["name"] == "knowledge_retriever_tool":
                    continue
                ret.append(
                    {
                        "key": one["name"],
                        "value": f"Tool Input:\n {one['input']}, Tool Output:\n {one['output']}",
                        "type": "tool",
                    }
                )
        return ret

    @staticmethod
    def _is_web_search_log(tool_log: dict) -> bool:
        name = tool_log.get("name")
        return name == "web_search" or name == "联网搜索"

    @staticmethod
    def _has_citation_key(output: Any) -> bool:
        return isinstance(output, str) and '"citation_key"' in output

    @classmethod
    def _dedupe_web_search_tool_logs(cls, tool_logs: list[dict]) -> list[dict]:
        web_search_indexes = [index for index, item in enumerate(tool_logs) if cls._is_web_search_log(item)]
        if len(web_search_indexes) <= 1:
            return tool_logs

        cited_indexes = [index for index in web_search_indexes if cls._has_citation_key(tool_logs[index].get("output"))]
        if not cited_indexes:
            return tool_logs

        keep_index = cited_indexes[-1]
        return [item for index, item in enumerate(tool_logs) if index == keep_index or index not in web_search_indexes]

    def _run_once(
        self, input_variable: str = None, unique_id: str = None, output_key: str = None, tool_invoke_list: list = None
    ) -> (str, str, list[CitationRegistryItemSchema]):
        """
        params:
            input_variable: Input variables, if yesbatchthen you need to pass in a variablekey, otherwiseNone
            unique_id: Node Execute Uniqueid
            output_key: Output Variableskey
            tool_invoke_list: Tool Call Log
        return:
            0: Output results to user
            1: Process of model thinking
        """
        # Description is a variable that references a batch, The value of the variable needs to be replaced with the variable selected by the user
        special_variable = f"{self.id}.batch_variable"
        variable_map = {}
        for one in self._user_variables:
            if input_variable and one == special_variable:
                variable_map[one] = self.get_other_node_variable(input_variable)
                continue
            variable_map[one] = self.get_other_node_variable(one)
        user = self._user_prompt.format(variable_map)
        self._user_prompt_list.append(user)

        chat_history: list[BaseMessage] = []
        if self._chat_history_flag:
            chat_history = self._get_chat_history_messages()

        llm_callback = LLMNodeCallbackHandler(
            callback=self.callback_manager,
            unique_id=unique_id,
            node_id=self.id,
            node_name=self.name,
            output=self._output_user,
            output_key=output_key,
            tool_list=tool_invoke_list,
            cancel_llm_end=True,
        )
        config = RunnableConfig(callbacks=[llm_callback])
        human_message = HumanMessage(content=[{"type": "text", "text": user}])
        human_message = self.contact_file_into_prompt(human_message, self._image_prompt)
        chat_history.append(human_message)
        logger.debug(f"agent invoke chat_history: {chat_history}")
        self._reset_citation_registry_items()

        if self._agent_executor_type == "ReAct":
            result = self._agent.invoke(
                {
                    "input": chat_history[-1].content,
                    "chat_history": chat_history[:-1],
                },
                config=config,
            )
            output = result["agent_outcome"].return_values["output"]
            if isinstance(output, dict):
                output = list(output.values())[0]
            round_messages = [human_message]
            round_messages.extend(_format_intermediate_steps(result.get("intermediate_steps", [])))
            round_messages.append(AIMessage(content=output))
            self._append_chat_history_messages(round_messages)
            return output, llm_callback.reasoning_content, self._collect_citation_registry_items()
        else:
            result = self._agent.invoke({"messages": chat_history}, config=config)
            result_messages = result["messages"]
            new_messages = [human_message]
            if len(result_messages) > len(chat_history):
                new_messages.extend(result_messages[len(chat_history) :])
            self._append_chat_history_messages(new_messages)
            return result_messages[-1].content, llm_callback.reasoning_content, self._collect_citation_registry_items()

    def _reset_citation_registry_items(self) -> None:
        for tool in self._citation_tools:
            tool.citation_registry_items = []

    def _collect_citation_registry_items(self) -> list[CitationRegistryItemSchema]:
        items: list[CitationRegistryItemSchema] = []
        for tool in self._citation_tools:
            items.extend(tool.citation_registry_items)
        return items
