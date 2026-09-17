# Design: F043 工作流助手节点 — 选表取 Schema + Schema 缓存

> **本文档定位 — 现状快照（Why this How）**
> - `spec.md` 回答做什么（AC-01~24）；本文回答**为什么这么实现**：决策、坑、契约。
> - `tasks.md` 是执行流水。实现变化时覆盖更新本文，只留"今天的状态"。

**关联**: [spec.md](./spec.md) · [tasks.md](./tasks.md)
**版本**: v2.6.0
**最后更新**: 2026-09-17

---

## 1. 目标与非目标

**目标**：在工作流「助手」节点（内部类型 `agent`）已有的数据库开关（`sql_agent`）上，增量提供三件事——配置态选表、运行态 schema 预取（省掉 SQL Agent 的 list→schema 多轮调用）、schema 的 Redis 缓存（TTL + 手动刷新 + 故障降级）。

**非目标**：独立数据库节点、数据源集中管理、执行任意 SQL 取数行、密码加密存储、SQL 写操作强拦截、独立助手应用改造、schema 自动失效探测、视图/存储过程选取（均见 spec 范围排除）。

---

## 2. 关键约束

遵循 `docs/constitution.md` C1–C7（尤其 C1 分层、C3 多租户、C5 错误码）。本功能特有约束：

- **同步执行环境**：工作流节点跑在 Celery `workflow_celery` 线程池（100 线程），`AgentNode.init_sql_agent_tool()` 是同步函数，只能用 `get_redis_client_sync()`，不能 await。
- **bisheng_langchain 包不能 import bisheng 主包**：现有 [load_tools.py](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng_langchain/gpts/load_tools.py) 与 [sql_agent/tool.py](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng_langchain/gpts/tools/sql_agent/tool.py) 都在独立的 `bisheng_langchain` 包，不依赖主包的 redis/租户上下文。**因此缓存读写、建连 URI 拼装、schema 获取必须发生在主包侧，langchain 包只接收"已经准备好的结果"**（决策 1）。
- **现有 SQL Agent 每次 invoke 都新建 engine、finally 中 dispose**（[tool.py L95-99](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng_langchain/gpts/tools/sql_agent/tool.py#L89-L100)），短连接模型；本期保持「配置态/预取即连即释」，不引入连接池复用。
- **RedisClient 用 pickle 序列化**（[redis_conn.py L57-70](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng/core/cache/redis_conn.py#L57-L70)）：缓存值只放 str/dict 等可 pickle 的简单结构，不放 engine/connection 对象。
- **DM8 连接约定**：`dm+dmPython://user:pass@host:port/?schema=SCHEMA`，库名走 query 的 `schema=`（dmPython 不接受 database 位，见 `scripts/migrate_mysql_to_dm.py` L562/L709）。
- **前端单文件 ≤600 行**（root AGENTS.md §4）：当前 SqlConfigItem 162 行，新增选表/缓存 UI 后若超限，抽子组件。
- **i18n**：所有新增文案走 `public/locales/{zh-Hans,en-US,ja}/flow.json`，注释用英文。

---

## 3. 方案对比与选定

### 决策 1：schema 预取与缓存放在主包侧，langchain 工具只被动接收

- **备选**：
  - A. **主包侧预取**：在 `AgentNode`（或主包新 service）里建连取 schema、读写缓存，把最终 schema 文本 + 选中表作为参数传给 langchain 的 `SqlAgentAPIWrapper` — 优点：langchain 包零侵入业务依赖、可用主包 Redis/租户上下文、缓存逻辑集中；缺点：要改 wrapper 的构造签名与 prompt。
  - B. **在 langchain 工具内部做缓存**：`SqlAgentAPIWrapper` 直接 import redis — 优点：改动集中在一个文件；缺点：违反包边界（langchain 包将依赖主包基础设施与租户 ContextVar），独立包无法单独分发，且测试困难。
- **选定**：A。
- **原因**：包边界是现有代码已确立的硬约束（langchain 包全文无 `from bisheng.` 主包导入）；缓存键需要 `tenant_id`（只有主包节点上下文有，见 [base.py](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng/workflow/nodes/base.py) kwargs）；放主包也便于配置态 API 与运行态复用同一个「建 URI → 建连 → 取表/取 schema」服务。
- **何时重新考虑**：若未来独立数据源模块落地、langchain 包也要独立复用缓存，再把 service 抽到双方都能依赖的位置（届时是独立 feature）。

### 决策 2：运行态如何"限定表 + 注入 schema"——双轨，优先预注入 prompt

- **备选**：
  - A. `SQLDatabase.from_uri(uri, include_tables=选中表, sample_rows_in_table_info=0)`，让现有 toolkit 的 list/schema 工具自然收窄 — 优点：改动最小；缺点：模型仍要调 list、逐表 schema，多轮调用仍在，缓存的 schema 文本用不上。
  - B. **预取 schema 文本 + 直接注入系统提示词 + 精简工具集**：主包取好 `schema_ddl` 传入 wrapper；选表路径下 wrapper 的系统 prompt 直接携带 DDL，工具集只保留执行查询工具（`sql_db_query`）+ 校验工具，去掉 `sql_db_list_tables`/`sql_db_schema` — 优点：ReAct 轮次从「list→N×schema→query」降到「query」，缓存命中时零连库，收益最大；缺点：要从 toolkit 里按名挑工具、维护两套 prompt。
  - C. 完全自研 SQL 执行工具，弃用 toolkit — 被否：重写方言/转义/行数限制逻辑，风险大。
- **选定**：B（选表路径）；**未选表路径保持现状**（AC-09，零改动行为）。
- **关键事实**：`SQLDatabaseToolkit.get_tools()` 返回的工具 `.name` 固定为 `sql_db_query`/`sql_db_list_tables`/`sql_db_schema`/`sql_db_query_checker`/`sql_db_query_sql_checker`（langchain_community 版本相关，实现时以实际版本为准并做防御性按名过滤）；`get_table_info` 对不存在的表会 `raise ValueError`（[sql_database.py L362-365](file:///e:/myCode/gitHub/bisheng/src/backend/.venv/Lib/site-packages/langchain_community/utilities/sql_database.py#L361-L366)），所以必须先取实际表交集（决策 5 的 AC-10 处理）。
- **何时重新考虑**：langchain 版本升级导致 toolkit 工具名变化时，按名过滤需同步（在 §5 登记为护栏，加防御：找不到 `sql_db_query` 时 fail loud）。

### 决策 3：缓存载体、键与 TTL

- **备选**：
  - A. **Redis 单键存整份 schema 文本**，key 含租户/连接指纹/表指纹 — 简单、一次 GET、天然原子；缺点：表范围任意变化都会产生新 key（旧 key 等 TTL 自然过期）。
  - B. 一表一键（hash 或每表独立 key），表范围变化可复用单表缓存 — 缓存命中率高；缺点：要管理 hash 结构、部分命中时还要回源补缺表，复杂度高，且刷新语义（整节点刷新 vs 单表）易混乱。
- **选定**：A（本期表数量预期为配置者勾选的有限集合，简单可控；B 留作 §8 后续改进）。
- **键规则**：
  `wf:sqlschema:{tenant_id}:{md5_hex}`，其中 md5 原文 = `dialect|host|port|db_name|db_username|` + `",".join(sorted(table_names))`。
  - **不含密码**（AC-19）；dialect 用归一化引擎名（mysql/postgresql/oracle/mssql/db2/gaussdb/dm）。
  - 端口缺省按方言默认端口参与归一化（与 URI 拼装同一函数产出）。
  - 未选表时不预取、不缓存（沿用运行时自查，AC-09），即**缓存只服务"选了表"的路径**。
- **值**：pickle 序列化的 dict `{"dialect": str, "tables": [表名...], "ddl": str, "fetched_at": iso8601}`；只含结构文本，无数据行。
- **TTL**：选定**固定默认 24 小时**，本期不暴露给前端配置（AC-15 取"固定默认值"方案；界面文案明示"缓存有效期 24 小时"）。常量集中定义便于调整。
- **原因**：schema 是低频变更数据，24h 在"新鲜度 vs 提效"间平衡；手动刷新兜底结构变更（AC-16）。

### 决策 4：配置态表清单的获取位置与接口形态

- **备选**：
  - A. 在现有 `/api/v1/workflow` router 加端点（如 `POST /workflow/db/tables`）— 与工作流配置同域，权限/登录中间件现成。
  - B. 新建独立 `db_inspect` 模块/router — 过度设计，本期不引入数据源域。
- **选定**：A。端点**无状态、不落库、不读工作流**：请求体直接带连接参数，服务端建连 → 取表名 → dispose → 返回。
- **安全**：该端点只做只读元数据查询（`get_usable_table_names()` 底层只查 information_schema / 方言等价物）；登录态必需；连接参数不打日志（AC-22）。
- **表清单上限/过滤（AC-05）**：服务端**不做硬截断**（避免漏表），由前端在返回清单内做关键字过滤；设置**软上限 2000 个**，超过时响应带 `truncated: true` 提示前端引导搜索（实际只在极端大库出现）。
- **超时（AC-06）**：`create_engine` 后用 `engine.connect(execution_timeout=...)` 不可移植，统一在 `create_engine` 的 `connect_args` 设 `connect_timeout=8`（mysql/pg）；其余方言用服务端包裹 + 前端请求超时双保险，常量 10s。

### 决策 5：失效表、故障降级、并发的行为口径

- **部分表不存在（AC-10）**：建连后先取 `get_usable_table_names()` 全集，与勾选表取交集；`missing = 勾选 - 实际`。missing 非空 → `logger.warning`（只记表名）；交集为空 → 抛 `DbSchemaNoValidTableError`（10563）；交集非空 → 只对交集 `get_table_info`。
- **Redis 故障（AC-17）**：读缓存失败 → 记 warning 后回源；写缓存失败 → 窄捕获 redis 异常 + warning，不影响返回（遵循后端 AGENTS.md「best-effort 缓存写」规范）。
- **并发（AC-23）**：不引入分布式锁。取 schema 只读幂等，允许并发重复回源；写缓存为整键 `setex` 覆盖，内容为同构 DDL，不会产生"错误内容"。同一次节点运行内，wrapper 只构造一次（见 4.1），无重复回源。
- **连接失败（AC-03）**：统一转业务错误 `DbConnectionFailedError`（10560）；驱动缺失（ImportError/ODBC）转 `DbDriverMissingError`（10561）；不向客户端泄露原始堆栈。

### 决策 6：前端表单扩展方式——在 sql_config 的 value 内增字段，不新增模板类型

- **选定**：继续复用 `type: "sql_config"` 这一个参数类型，在其 `value` 对象内新增 `selected_tables`、`schema_cache_enabled`；不新增节点参数类型（避免改 Parameter 分发与模板结构）。组件内部按开关条件渲染「获取表列表」按钮、表多选、缓存开关、刷新缓存按钮。
- **存量兼容（AC-20）**：读配置时 `selected_tables ?? []`、`schema_cache_enabled ?? false` 兜底；template.json 与前端 workflow.ts 的默认值同步加这两个字段。

---

## 4. 系统现状与设计（接手必读）

### 4.1 现状数据流（改造前）

```
画布保存 sql_agent 配置(open/engine/addr/db/user/pwd)
  → AgentNode.__init__: SqlAgentParams.model_validate → _init_sql_address() 拼 URI
  → init_sql_agent_tool(): load_tools({"sql_agent": {llm, sql_address}})
  → SqlAgentAPIWrapper.__init__: SQLDatabase.from_uri(uri) + SQLDatabaseToolkit + create_react_agent
     系统 prompt 强制: list_tables → 逐表 schema → 写 SELECT → sql_db_query → 作答
  → SqlAgentTool._run: agent.invoke() ; finally engine.dispose()
```

关键文件：
- 参数与 URI 拼装：[agent.py L147-169](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng/workflow/nodes/agent/agent.py#L147-L169)（`SqlAgentParams`）、[L401-509](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng/workflow/nodes/agent/agent.py#L401-L509)（`_parse_db_host_port` / `_init_sql_address`）
- 工具装配：[agent.py L331-335](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng/workflow/nodes/agent/agent.py#L331-L335)（`init_sql_agent_tool`）
- 注册：[load_tools.py L96-97,L127](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng_langchain/gpts/load_tools.py#L96-L127)（`sql_agent` 必填 `['llm','sql_address']`，可选列表为 `[]`）
- 工具实现：[tool.py](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng_langchain/gpts/tools/sql_agent/tool.py)
- 前端表单：[SqlConfigItem.tsx](file:///e:/myCode/gitHub/bisheng/src/frontend/platform/src/pages/BuildPage/flow/FlowNode/component/SqlConfigItem.tsx)；类型分发 [Parameter.tsx L210-215](file:///e:/myCode/gitHub/bisheng/src/frontend/platform/src/pages/BuildPage/flow/FlowNode/Parameter.tsx#L210-L215)
- 模板：[template.json L28704-28720](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng/database/data/template.json#L28704-L28720)；前端镜像 [workflow.ts L672-687](file:///e:/myCode/gitHub/bisheng/src/frontend/platform/src/controllers/API/workflow.ts#L672-L687)

### 4.2 改造后结构

新增主包服务 `src/backend/bisheng/workflow/nodes/agent/db_schema_service.py`（放在 agent 节点包内，属 workflow 引擎内部能力，不新开域、不加 router 子包）：

| 函数 | 职责 |
|---|---|
| `build_sql_uri(params: SqlAgentParams) -> str` | 从 `_init_sql_address()` 迁移/复用六+一方言 URI 拼装（**新增 dm 分支**），并产出归一化 dialect 与 `(host,port,db,user)` 指纹要素；AgentNode 的 `_init_sql_address` 改为调它，消除重复 |
| `list_tables(params, timeout=10) -> list[str]` | 建连取表名（`sample_rows_in_table_info` 无关，仅用 inspector），finally dispose；供配置态 API 调用 |
| `get_schema_ddl(uri, dialect, tables) -> (found, missing, ddl)` | `from_uri(uri, include_tables=tables, sample_rows_in_table_info=0)`，取交集后 `get_table_info(found, get_col_comments=<方言支持>)`，finally dispose；纯实时、无缓存 |
| `get_schema_with_cache(params, tenant_id) -> dict` | 算 key → 读 Redis（失败降级）→ miss 调 `get_schema_ddl` → 写 Redis（失败仅 warning）→ 返回 `{ddl, tables, from_cache}` |
| `refresh_schema_cache(params, tenant_id) -> dict` | 删 key（忽略不存在）→ 强制 `get_schema_ddl` → 重建缓存 |
| `build_cache_key(...) -> str` / 常量 `SCHEMA_CACHE_TTL=86400` / key 前缀 | 决策 3 |

> 注：该 service 位于 workflow 包、同步实现；配置态 API 是 async FastAPI 端点，用 `anyio.to_thread.run_sync`（项目 FastAPI 自带 anyio）或直接在线程中调用（SQLAlchemy 同步调用是阻塞的，放线程池避免堵事件循环——实现时统一用 `await run_sync` 风格，参考仓库其他同步阻塞调用惯例）。

langchain 侧改造 [tool.py](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng_langchain/gpts/tools/sql_agent/tool.py)：

- `SqlAgentAPIWrapper` 新增可选入参：`selected_tables: list[str] | None`、`schema_ddl: str | None`。
- 有 `schema_ddl`（选表路径）：`create_react_agent` 用新 prompt（直接内嵌 DDL，指示"以下是可用表完整结构，直接写 SELECT，无需再获取表结构"），工具集按名过滤只留查询/校验类；`SQLDatabase.from_uri(uri, include_tables=selected_tables, sample_rows_in_table_info=0)` 双保险收窄可执行范围。
- 无 `schema_ddl`：完全走现有构造与 prompt（AC-09）。
- [load_tools.py L127](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng_langchain/gpts/load_tools.py#L127) 把 `selected_tables`、`schema_ddl` 加入 `sql_agent` 的**可选参数**列表（必填仍 `['llm','sql_address']`，保证其他调用方不传时不报错）。

AgentNode 侧改造：

- `SqlAgentParams` 增字段：`selected_tables: list[str] = []`、`schema_cache_enabled: bool = False`（pydantic 兜底，AC-20）。
- `init_sql_agent_tool()`：当 `open and selected_tables` → 先 `get_schema_with_cache(...)`（传入 `self.tenant_id`），再把 `selected_tables` + `schema_ddl` 放进 tool_params；未选表 → 维持原调用。
- URI 构造复用 `build_sql_uri`（替代 `_init_sql_address`，DM8 分支在 service 内补齐）。

### 4.3 对外契约（HTTP API）

路由文件：在 [api/v1/workflow.py](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng/api/v1/workflow.py)（prefix `/workflow`）新增两个端点，均需登录态：

**① 获取表列表**

```
POST /api/v1/workflow/db/tables
body: {
  "database_engine": "mysql|postgresql|oracle|sqlserver|db2|gaussdb|dm",
  "db_address": "host:port", "db_name": "...", "db_username": "...", "db_password": "..."
}
resp 200: { "data": { "tables": ["t1","t2"], "truncated": false } }
```

**② 刷新 schema 缓存**

```
POST /api/v1/workflow/db/schema/refresh
body: { ...同上连接字段..., "selected_tables": ["t1","t2"] }
resp 200: { "data": { "tables": [...实际存在], "missing_tables": [...], "fetched_at": "..." } }
```

> 刷新端点为什么也要带连接参数而非节点 id：节点配置尚未保存时（画布编辑中）就允许刷新；保持端点无状态、不落库，与 ① 同构。权限上仅要求登录（配置动作发生在有工作流编辑权限的画布内，前端只在编辑态暴露入口）。

### 4.4 前端改动

| 文件 | 改动 |
|---|---|
| [SqlConfigItem.tsx](file:///e:/myCode/gitHub/bisheng/src/frontend/platform/src/pages/BuildPage/flow/FlowNode/component/SqlConfigItem.tsx) | open 态下新增：「获取表列表」按钮（带 loading/失败 toast）、表多选（复选下拉，支持关键字搜索；展示 missing 态）、「Schema 缓存」开关、「刷新缓存」按钮；字段写入 `value.selected_tables` / `value.schema_cache_enabled`；超 600 行则抽 `DbTableMultiSelect.tsx` 子组件 |
| [workflow.ts (API)](file:///e:/myCode/gitHub/bisheng/src/frontend/platform/src/controllers/API/workflow.ts) | 新增 `getDbTables(payload)`、`refreshDbSchema(payload)` 调 `/api/v1/workflow/db/*`；默认 sql_config value 加两字段 |
| [template.json](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng/database/data/template.json) | sql_config 默认 value 增加 `"selected_tables": [], "schema_cache_enabled": false`；数据库类型可选值无需改（engine 列表由前端常量维护，需加 DM8/达梦） |
| `public/locales/{zh-Hans,en-US,ja}/flow.json` | 新增文案：获取表列表、表选择占位、缓存开关、24h 提示、刷新缓存、连接失败/超时/缺驱动/无有效表等 |

前端 DATABASE_OPTIONS 增加 `'DM8'`（当前 [SqlConfigItem L16](file:///e:/myCode/gitHub/bisheng/src/frontend/platform/src/pages/BuildPage/flow/FlowNode/component/SqlConfigItem.tsx#L16) 仅 6 种；注意后端 `SqlAgentParams.validate_database_engine` 白名单同步加 `dm`，大小写归一化已有 `.lower()`）。

### 4.5 方言适配矩阵（`build_sql_uri`）

| 配置者选 engine | SQLAlchemy URI | 默认端口 | db_name 语义 | 备注 |
|---|---|---|---|---|
| mysql | `mysql+pymysql` | 3306 | database | `charset=utf8mb4`；connect_args `connect_timeout` |
| postgresql | `postgresql+psycopg2` | 5432 | database | 兼容现有 `postgres/postgresql` |
| oracle | `oracle+oracledb` | 1521 | service_name（走 query） | 沿用现状 |
| sqlserver | `mssql+pyodbc` | 1433 | database | `ODBC Driver 18` + `TrustServerCertificate=yes`；缺驱动→10561 |
| db2 | `db2+ibm_db` | 50000 | database | 沿用现状 |
| gaussdb | `opengauss+psycopg2` | 5432 | database | 沿用现状 |
| **dm（新增）** | `dm+dmPython` | 5236 | **schema（走 query `schema=`）** | 不用 database URL 位（dmPython 拒绝） |

---

## 5. 已知坑 / 反直觉事实

| # | 反直觉事实 | 如果不知道会怎样 | 在哪处理 |
|---|---|---|---|
| 1 | langchain 包不能 import 主包（无 `from bisheng.`），但缓存又需要 tenant_id | 在 tool.py 里直接取 Redis/租户会打破包边界、循环依赖 | 决策 1：预取/缓存全在主包 db_schema_service，langchain 只收 ddl |
| 2 | `get_table_info(table_names=...)` 遇到任一不存在的表直接 `ValueError`，不返回部分 | 表配置后被删会导致整个节点报错、问答中断 | 先 `get_usable_table_names()` 取交集，missing 仅 warning，全丢才报 10563（决策 5） |
| 3 | `SQLDatabase` 默认 `sample_rows_in_table_info=3`，会查 3 行样例数据拼进 schema | 缓存内容会含业务数据行，违反 AC-19，也增加泄露面 | 预取/执行两处都显式 `sample_rows_in_table_info=0` |
| 4 | `from_uri(include_tables=...)` 只在 SQLDatabase 层收窄，真正执行仍靠 prompt 约束 | 误以为 include_tables 就能阻止 LLM 查别的表 | 选表路径同时精简工具集 + prompt 明示（决策 2）；只读仍非强拦截（spec 已排除） |
| 5 | 现有 sql_agent 密码明文拼 URI 且随节点 JSON 落库 | 把 uri 整个拿来做缓存 key/日志会泄露密码 | 缓存 key 只用 host/port/db/user/表指纹；日志禁止 uri/密码（AC-19/22） |
| 6 | DM8 的 dmPython 不接受 URL 的 database 位 | `dm+...//host:port/DBNAME` 直接建连失败 | 用 `?schema=xxx`（4.5 矩阵），迁移脚本已有同样结论 |
| 7 | RedisClient.get/set 是 pickle 序列化，且 set 默认 expiration=3600 | 塞 engine 对象会 pickle 失败；忘传 TTL 会变成 1h 而非 24h | 只缓存 dict；显式传 `SCHEMA_CACHE_TTL` |
| 8 | toolkit 工具名随 langchain_community 版本可能变化 | 按名过滤工具会漏/错 | 按名选择 + 找不到 `sql_db_query` 时显式报错（fail loud），不静默空工具 |
| 9 | `SqlAgentTool._run` finally 里 dispose engine | 预取连接若复用同一 engine 会被提前关掉 | 预取是独立短连接（取完即 dispose），与执行期 engine 生命周期分离 |
| 10 | 前端 engine 常量只有 6 个、后端白名单也只有 6 个 | 选 DM8 会被 validator 拒 | 前后端同步加 dm/DM8（4.4） |

---

## 6. 对外契约与依赖

### 6.1 我提供给别人的（Outgoing）

| 契约 | 形式 | 谁在用 |
|---|---|---|
| `POST /api/v1/workflow/db/tables` | HTTP（登录态） | platform 画布 SqlConfigItem |
| `POST /api/v1/workflow/db/schema/refresh` | HTTP（登录态） | platform 画布刷新按钮 |
| 节点 sql_config 新增字段 `selected_tables`/`schema_cache_enabled` | 工作流 JSON 配置契约（向后兼容，默认空/关） | 工作流保存/版本/复制链路（均透传 JSON，无破坏） |
| `SqlAgentAPIWrapper` 新增可选入参 `selected_tables`/`schema_ddl` | Python 内部 API | 仅 AgentNode / load_tools；可选参数不破坏其他 sql_agent 调用方 |

### 6.2 我依赖别人的（Incoming）

| 依赖 | 风险点 |
|---|---|
| `langchain_community.SQLDatabase / SQLDatabaseToolkit` | 版本升级可能改工具名/签名（坑 8）；锁定当前 uv.lock 版本行为 |
| Redis（`get_redis_client_sync`） | 故障需降级（AC-17），非硬依赖 |
| 各数据库 Python 驱动（pymysql/psycopg2/ibm_db/oracledb/pyodbc/dmPython/opengauss） | 镜像内已含除 ODBC Driver 外的库；SQLServer 还需系统级 ODBC Driver 18（部署前置） |
| 工作流节点上下文 `tenant_id` kwarg | 由 graph_engine 注入（[node_manage](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng/workflow/nodes/node_manage.py) 统一传），缓存隔离依赖它 |

---

## 7. 测试与可观测

**单元测试（新增 `src/backend/test/workflow/` 下，遵循 asyncio_mode=auto、按模块建目录约定）**
- `build_sql_uri`：七方言 URI 产出与默认端口、DM8 schema 传参、地址无端口兜底。
- 缓存键：相同连接+表（不同顺序）同 key；不同租户/库/表/用户不同 key；key 中无密码。
- `get_schema_ddl`：用 sqlite 或 mock 的 SQLDatabase 验证交集/缺失表逻辑（全缺失抛 10563）；`sample_rows_in_table_info=0` 断言 DDL 不含样例行。
- 缓存：命中不回源（mock 建连计数）、miss 回源并写缓存、Redis 异常降级、refresh 强制回源。
- wrapper：传 schema_ddl 时工具集不含 list/schema、prompt 含 DDL；不传时与现状一致。

**集成/手工验证**
- MySQL/PostgreSQL 真实库走通：配置态拉表 → 勾选 → 运行问答（观察日志中工具调用从 3+ 轮降为 1 轮）→ 二次运行命中缓存（日志 from_cache）→ 改表结构后刷新缓存生效。
- SQLServer 在装有 ODBC Driver 18 的环境验证；缺驱动环境验证 10561 提示。
- DM8 连真实实例验证 `?schema=` 建连与表清单（若环境暂缺，列为联调依赖项并在 tasks 标注）。

**可观测**：`loguru` 结构化日志（`{}` 占位符）记录 `act=sql_schema_fetch tenant=.. engine=.. tables=N missing=M from_cache=True/False`；**不含**密码/uri。缓存降级 warning、缺驱动 warning。

**前端**：`npm run lint` / tsc 无新增报错；i18n 三语补全。

---

## 8. 后续改进 / 不打算做的事

- 一表一键缓存（决策 3 方案 B）：表范围频繁微调时提升命中率，待有真实诉求再做。
- schema 自动失效（监听 DDL / 版本号探测）：当前 TTL+手动刷新足够。
- 独立数据源管理 + 密码 Fernet 加密：另立 feature；届时本 service 的 URI/取数函数可下沉复用。
- SQL 写操作服务端强拦截（SQL 解析只允许 SELECT / 只读账号托管）。
- 视图/物化视图选取：`SQLDatabase(view_support=True)` 已支持，待交互需要时开放开关。

---

## 修订历史

| 日期 | 改动 | 触发原因 |
|---|---|---|
| 2026-09-17 | 初版 | F043 设计定稿，待评审 |
