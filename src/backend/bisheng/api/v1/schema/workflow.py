from enum import Enum
from typing import Optional, Any, List

from pydantic import BaseModel, Field, field_validator


class WorkflowEventType(Enum):
    NodeRun = 'node_run'
    # Ice Breaker 
    GuideWord = 'guide_word'
    # Facilitation Questions
    GuideQuestion = 'guide_question'
    # Inform the user that user input is now required
    UserInput = 'input'
    # Output events that return predefined content to the user
    OutputMsg = 'output_msg'
    # Output requires user input at the same time
    OutputWithInput = 'output_with_input_msg'
    # Output requires user selection at the same time
    OutputWithChoose = 'output_with_choose_msg'
    # Streaming output events, including streaming process, streaming end two states
    StreamMsg = 'stream_msg'
    Close = 'close'
    Error = 'error'


class WorkflowOutputSchema(BaseModel):
    message: Any = Field(default=None, description='The message content')
    reasoning_content: Optional[str] = Field(default=None, description='The reasoning content')
    output_key: Optional[str] = Field(default=None, description='output message key')
    files: Optional[List[Any]] = Field(default=None, description='The files list')
    extra: Optional[str] = Field(default=None, description='The extra data')


class WorkflowInputItem(BaseModel):
    key: str = Field(default=None, description='Unique key corresponding to user input')
    type: str = Field(default=None, description='The input type, select or dialog or file')
    value: Any = Field(default=None, description='The input default value')
    label: str = Field(default=None, description='The key label')
    multiple: bool = Field(default=False, description='The input is multi select')
    required: bool = Field(default=False, description='The input is required')
    options: Optional[Any] = Field(default=None, description='The select type options')
    file_type: Optional[str] = Field(default=None, description='The allow upload file type')


class WorkflowInputSchema(BaseModel):
    input_type: str = Field(default=None, description='The judge user input is dialog or form')
    value: List[WorkflowInputItem] = Field(default=None, description='The input schema items')


class WorkflowEvent(BaseModel):
    event: str = Field(default=None, description='The event type')
    message_id: Optional[str] = Field(default=None, description='message id for save into mysql')
    status: Optional[str] = Field(default='end', description='The event status')
    node_id: Optional[str] = Field(default=None, description='The node id')
    node_name: Optional[str] = Field(default=None, description='The node name')
    node_execution_id: Optional[str] = Field(default=None, description='The node exec unique id')
    output_schema: Optional[WorkflowOutputSchema] = Field(default=None, description='The output schema')
    input_schema: Optional[WorkflowInputSchema] = Field(default=None, description='The input schema')

    @field_validator('message_id', mode='before')
    @classmethod
    def validate_message_id(cls, v: Any) -> Optional[str]:
        if isinstance(v, str) or v is None:
            return v
        return str(v)


class WorkflowStream(BaseModel):
    session_id: str = Field(default=None, description='The session id')
    data: WorkflowEvent | list[WorkflowEvent] = Field(default=None, description='The event data or event data list')


# ---------------------------------------------------------------------------
# F043: stateless SQL database inspection endpoints (assistant node)
# ---------------------------------------------------------------------------

class DbTableListRequest(BaseModel):
    """Connection params for listing tables at config time (never persisted)."""

    database_engine: str = Field(
        default='mysql',
        description='mysql, postgresql, oracle, sqlserver, db2, gaussdb, dm',
    )
    db_address: str = Field(..., description='host:port')
    db_name: str = Field(..., description='database/schema name')
    db_username: str = Field(..., description='database user')
    db_password: str = Field(..., description='database password')


class DbTableListResponse(BaseModel):
    tables: List[str] = Field(default_factory=list, description='Visible table names')
    truncated: bool = Field(default=False, description='Soft limit (2000) reached')


class DbSchemaRefreshRequest(DbTableListRequest):
    selected_tables: List[str] = Field(
        default_factory=list, description='Tables whose schema should be prefetched')
    schema_cache_enabled: bool = Field(
        default=False, description='Rebuild the Redis cache when True')
    schema_cache_ttl: int = Field(
        default=24, ge=1, le=720, description='Cache lifetime in hours (default 24)')


class DbSchemaRefreshResponse(BaseModel):
    tables: List[str] = Field(default_factory=list, description='Tables that exist')
    missing_tables: List[str] = Field(default_factory=list, description='Tables not found')
    fetched_at: Optional[str] = Field(default=None, description='ISO8601 fetch timestamp')
    from_cache: bool = Field(default=False, description='Served from cache')
