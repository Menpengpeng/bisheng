# Tasks: F043 工作流助手节点 — 选表取 Schema + Schema 缓存

**关联规格**: [spec.md](./spec.md) · **设计真相**: [design.md](./design.md)
**版本**: v2.6.0

---

## 状态

| 步骤 | 状态 | 备注 |
|------|------|------|
| spec.md | ✅ 已评审 | 用户 2026-09-17 确认 |
| design.md | 🔲 草稿 | 用户确认后更新本行 |
| tasks.md | 🔲 草稿 | 评审确认后进入实现 |
| 实现 | 🔲 未开始 | 0 / 13 完成 |

---

## 开发模式

- 本特性**无新表、无 Alembic、无新领域对象**；改动集中在 workflow 引擎内部 service + langchain sql_agent 工具 + 2 个只读 API + 一个前端表单组件。
- 后端 Test-First：纯函数（URI 拼装、缓存键）优先写单测；SQLDatabase 交互用 mock/fake 验证交集与降级逻辑。测试放 `src/backend/test/workflow/`（按模块建目录）。
- 前端手动验证（按 platform AGENTS.md，dev :3001）。
- 七方言真实联调按环境可用性推进，MySQL/PostgreSQL 为必测，SQLServer/DM8 依赖外部环境，标注联调风险。

---

## Tasks

### Wave 1 — 后端基础设施（无外部依赖，可并行）

- [ ] **T001**: 错误码定义
  **文件**: `src/backend/bisheng/common/errcode/flow.py`
  **逻辑**: 在 105 段（workflow）新增，避开已用 10550/10599：
  - `DbConnectionFailedError = 10560`（连接失败：地址不通/账号密码错/库不存在，AC-03）
  - `DbDriverMissingError = 10561`（数据库驱动缺失，如 ODBC Driver 18，AC-02）
  - `DbInspectTimeoutError = 10562`（建连/取元数据超时，AC-06）
  - `DbSchemaNoValidTableError = 10563`（勾选表全部不存在，AC-10）
  **覆盖 AC**: AC-02, AC-03, AC-06, AC-10
  **依赖**: 无

- [ ] **T002**: 方言 URI 服务（含 DM8）
  **文件**: 新建 `src/backend/bisheng/workflow/nodes/agent/db_schema_service.py`
  **逻辑**:
  - `build_sql_uri(params) -> URIResult(dialect, uri, host, port, db_name, username)`：把 [agent.py L401-509](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng/workflow/nodes/agent/agent.py#L401-L509) 六方言逻辑迁移进来（行为保持），**新增 dm 分支**：`dm+dmPython`、默认端口 5236、db_name 走 `query={"schema": db_name}`，不用 database 位（design §4.5 / 坑 6）
  - 保留 host:port 缺省端口兜底；归一化 dialect 枚举：mysql/postgresql/oracle/mssql/db2/gaussdb/dm
  - 日志中不输出密码/完整 uri（坑 5）
  - AgentNode._init_sql_address 改为薄封装调用本函数（消除重复）
  **覆盖 AC**: AC-24
  **依赖**: 无

- [ ] **T003**: T002 单元测试
  **文件**: 新建 `src/backend/test/workflow/__init__.py`、`src/backend/test/workflow/test_db_schema_uri.py`
  **逻辑**: 七方言 URI 断言（driver、默认端口、oracle service_name、dm 走 schema=、地址无端口兜底、postgres/postgresql 归一）；断言返回结构不含密码以外字段被误打印（key 相关在 T005）
  **依赖**: T002

### Wave 2 — 后端 schema 获取 + 缓存

- [ ] **T004**: schema 获取与缓存核心（先测试，T005 配对）
  **文件**: `src/backend/bisheng/workflow/nodes/agent/db_schema_service.py`
  **逻辑**（全部同步实现）:
  - 常量 `SCHEMA_CACHE_PREFIX = "wf:sqlschema:"`、`SCHEMA_CACHE_TTL = 86400`、`TABLE_LIST_SOFT_LIMIT = 2000`、`CONNECT_TIMEOUT_SECONDS = 10`
  - `build_cache_key(tenant_id, uri_meta, table_names) -> str`：md5 原文 `dialect|host|port|db_name|db_username|sorted(tables)`，**不含密码**（坑 5），key = 前缀 + tenant_id + ":" + md5
  - `list_tables(params) -> tuple[list[str], bool]`：建连取 `get_usable_table_names()`，finally `engine.dispose()`（坑 9：独立短连接）；超软限返回 truncated=True；异常归一：ImportError/驱动类→10561，超时→10562，其他连接类→10560
  - `get_schema_ddl(uri_meta_or_params, tables) -> SchemaResult(found, missing, ddl, fetched_at)`：`SQLDatabase.from_uri(uri, include_tables=tables, sample_rows_in_table_info=0)`（坑 3 必须为 0）；先取实际表全集做交集（坑 2，`get_table_info` 遇缺失表直接 ValueError），missing 非空 logger.warning 只记表名；found 为空 → 10563；`get_table_info(found, get_col_comments=...)`（mysql/pg/oracle 传 True，其余 False 防御）
  - `get_schema_with_cache(params, tenant_id) -> SchemaResult`：开缓存才读；Redis get 异常→warning 回源（AC-17）；miss→get_schema_ddl→写缓存 dict `{dialect,tables,ddl,fetched_at}`（坑 7：显式 TTL、只 pickle dict），写失败窄捕获 warning 不抛；命中结果带 `from_cache=True`
  - `refresh_schema_cache(params, tenant_id) -> SchemaResult`：delete key（不存在忽略）→ 强制 get_schema_ddl → 重建
  - 建连统一 `sample_rows_in_table_info=0`；mysql/pg connect_args 加 connect_timeout
  **覆盖 AC**: AC-02, AC-03, AC-05, AC-06, AC-10, AC-13, AC-14, AC-15, AC-17, AC-18, AC-19, AC-21, AC-23
  **依赖**: T001, T002

- [ ] **T005**: schema/缓存服务单元测试
  **文件**: `src/backend/test/workflow/test_db_schema_service.py`
  **逻辑**（mock `SQLDatabase.from_uri` / fake redis client）:
  - 缓存键：表顺序无关同 key；租户/库/用户/表不同则不同；key 与 value 均不含密码
  - 命中：不调用建连（from_cache=True）；miss：调用并写缓存且 TTL=86400
  - Redis get/set 抛异常：降级回源成功、不抛出
  - 交集：部分缺失 warning 且只返回存在表；全缺失抛 10563
  - refresh：先 delete 再回源重建
  - list_tables 异常归一（驱动缺失 10561 / 超时 10562 / 连接失败 10560）
  **依赖**: T004

### Wave 3 — langchain 工具改造

- [ ] **T006**: SqlAgentAPIWrapper 支持选表 + 注入 schema
  **文件**: `src/backend/bisheng_langchain/gpts/tools/sql_agent/tool.py`
  **逻辑**:
  - 入参新增可选 `selected_tables: list[str] | None = None`、`schema_ddl: str | None = None`
  - 建 `SQLDatabase.from_uri` 时：有 selected_tables 则 `include_tables=selected_tables, sample_rows_in_table_info=0`（坑 3/4）
  - 有 schema_ddl：新增选表版系统 prompt——内嵌 DDL，指令"以下是可用表完整结构，直接写 {dialect} SELECT，无需再获取表列表/结构"，保留仅 SELECT/限 50 行/禁 DML（AC-11）；工具集按名过滤，只保留 `sql_db_query`（及 query checker，存在则留），**剔除** `sql_db_list_tables`/`sql_db_schema`；找不到 `sql_db_query` 直接抛错（坑 8 fail loud）
  - 无 schema_ddl：现有 prompt 与全量工具完全不变（AC-09）
  **覆盖 AC**: AC-07, AC-08, AC-09, AC-11
  **依赖**: 无（与 Wave 2 可并行，契约只认两个入参）

- [ ] **T007**: load_tools 注册可选参数 + 单测
  **文件**: `src/backend/bisheng_langchain/gpts/load_tools.py`、新建 `src/backend/test/workflow/test_sql_agent_tool.py`
  **逻辑**: [L127](file:///e:/myCode/gitHub/bisheng/src/backend/bisheng_langchain/gpts/load_tools.py#L127) `sql_agent` 可选参数列表加 `selected_tables`、`schema_ddl`（必填仍仅 llm/sql_address，保证其他调用方兼容）；单测构造 fake llm + sqlite 内存库验证：传 ddl 时工具无 list/schema 且 prompt 含 DDL；不传时工具数量与现状一致
  **依赖**: T006

### Wave 4 — AgentNode 接线

- [ ] **T008**: SqlAgentParams 扩展 + init_sql_agent_tool 接线
  **文件**: `src/backend/bisheng/workflow/nodes/agent/agent.py`
  **逻辑**:
  - `SqlAgentParams` 加 `selected_tables: list[str] = []`、`schema_cache_enabled: bool = False`；engine validator 白名单加 `"dm"`（AC-20 靠默认值兜底）
  - `init_sql_agent_tool()`：open 时先用 build_sql_uri 取 uri_meta；当 selected_tables 非空 → `get_schema_with_cache(params, self.tenant_id)`，tool_params 增加 `selected_tables` + `schema_ddl=result.ddl`；为空则维持原调用（AC-09/AC-12 关闭语义）
  - `_init_sql_address` 复用 T002 的 build_sql_uri
  - 运行日志：from_cache / 表数 / missing（不含敏感信息）
  **覆盖 AC**: AC-07~AC-14, AC-20, AC-21, AC-23
  **依赖**: T002, T004, T007

### Wave 5 — 配置态 API

- [ ] **T009**: 表清单 + 刷新缓存端点
  **文件**: `src/backend/bisheng/api/v1/workflow.py`（复用现有 `/workflow` router，不新增模块）；请求 schema 就近定义在 `src/backend/bisheng/api/v1/schema/workflow.py`（已存在则追加）
  **逻辑**:
  - `POST /workflow/db/tables`：body = 连接五要素（engine/addr/db/user/pwd），`UserPayload.get_login_user` 必需；同步阻塞调用放线程（anyio `run_sync` 封装，堵事件循环）；返回 `{tables, truncated}`（AC-01/05）
  - `POST /workflow/db/schema/refresh`：body 再加 `selected_tables`；调 `refresh_schema_cache(params, tenant_id=login_user 当前租户)`，返回 `{tables, missing_tables, fetched_at}`（AC-16）
  - service 抛 10560-10563 时用 `return_resp`/错误码既有转换返回，不泄露堆栈（AC-03）
  - 端点不落库、不读 flow；禁止记录密码/uri（AC-22）
  **覆盖 AC**: AC-01~AC-06, AC-16, AC-22
  **依赖**: T004, T008

- [ ] **T010**: API 测试
  **文件**: `src/backend/test/workflow/test_db_inspect_api.py`
  **逻辑**：mock db_schema_service，验证未登录 401、参数缺失 422、成功结构、错误码透传（10560/10563）；**测试降级**：真实建连在手工联调覆盖
  **依赖**: T009

### Wave 6 — 前端 Platform

- [ ] **T011**: API 模块 + 节点默认值
  **文件**: `src/frontend/platform/src/controllers/API/workflow.ts`
  **逻辑**: 增 `getDbTables(payload)`、`refreshDbSchema(payload)`（走 `@/controllers/request`，路径 `/api/v1/workflow/db/*`）；sql_config 默认 value 增加 `selected_tables: []`、`schema_cache_enabled: false`；DATABASE_OPTIONS 常量来源处加 `'DM8'`
  **依赖**: T009

- [ ] **T012**: SqlConfigItem 选表 + 缓存 UI
  **文件**: `src/frontend/platform/src/pages/BuildPage/flow/FlowNode/component/SqlConfigItem.tsx`（超 600 行则抽 `DbTableMultiSelect.tsx`）
  **逻辑**:
  - open 态新增「获取表列表」按钮：调 getDbTables，loading 态 + 成功后弹出复选下拉（支持表名关键字搜索，AC-05），失败 toast 显示后端错误消息（连接失败/超时/缺驱动）
  - 已选表持久化到 `value.selected_tables`；重新拉表后保留仍存在的勾选，缺失表置顶标红提示（AC-04）
  - 「Schema 缓存」开关 → `value.schema_cache_enabled`，旁注"有效期 24 小时"（AC-15）；「刷新缓存」按钮 → refreshDbSchema，成功/失败 toast（AC-16）
  - 现有校验（非空/长度）保持；开关关闭时不展示新增区块
  - i18n 文案补 `public/locales/{zh-Hans,en-US,ja}/flow.json`
  - 后端模板镜像：`src/backend/bisheng/database/data/template.json` sql_config 默认值同步两字段（注意该文件编码，局部 patch）
  **覆盖 AC**: AC-01~AC-05, AC-12, AC-15, AC-16, AC-20
  **手动验证**:
  - `npm start`（:3001）打开含助手节点的工作流，开数据库开关
  - 填 MySQL 连接 → 获取表列表 → 勾选 → 保存 → 刷新页面回显勾选
  - 开缓存 → 运行两次工作流，后端日志第二次 from_cache=True
  - 改错密码 → 获取表列表报连接失败；删/改选表后缓存按新范围
  - `npm run lint` 与 tsc 无新增错误
  **依赖**: T011

### Wave 7 — 联调与收尾

- [ ] **T013**: 真实库联调 + 静态检查 + 契约登记核对
  **逻辑**:
  - MySQL/PostgreSQL 真实库端到端（选表问答轮次下降、缓存命中、刷新、表被删的 missing 行为）
  - SQLServer（ODBC Driver 18 环境）/ DM8（`?schema=`）按可用环境验证；环境缺失时在 tasks 记录联调遗留，不阻塞代码合并
  - 后端 `uv run ruff check` + 相关 pytest 全绿；前端 lint/tsc
  - 核对 release-contract.md 表 1/表 3/模块编码表 F043 登记已完成（T001 同步）
  **依赖**: T008, T010, T012

---

## 实际偏差记录

> 推翻已 ★ 确认的决策时，先停下与用户重新确认，再记录并回写 design.md。

- （实现期填写）
