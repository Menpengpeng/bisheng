from .base import BaseErrorCode


# Skill service related return error code, function module code:105
class NotFoundVersionError(BaseErrorCode):
    Code: int = 10500
    Msg: str = 'Skill version information not found'


class CurVersionDelError(BaseErrorCode):
    Code: int = 10501
    Msg: str = 'Version currently in use cannot be deleted'


class VersionNameExistsError(BaseErrorCode):
    Code: int = 10502
    Msg: str = 'Version name already exists'


class WorkFlowOnlineEditError(BaseErrorCode):
    Code: int = 10525
    Msg: str = 'Workflow is live and not editable'


class WorkFlowInitError(BaseErrorCode):
    Code: int = 10526
    Msg: str = 'Workflow initialization failed'


class WorkFlowWaitUserTimeoutError(BaseErrorCode):
    Code: int = 10527
    Msg: str = 'Workflow timed out waiting for user input'


class WorkFlowNodeRunMaxTimesError(BaseErrorCode):
    Code: int = 10528
    Msg: str = 'Node exceeds maximum number of executions'


class WorkflowNameExistsError(BaseErrorCode):
    Code: int = 10529
    Msg: str = 'Duplicate workflow name'


class FlowTemplateNameError(BaseErrorCode):
    Code: int = 10530
    Msg: str = 'Template Name Already Exists'


class WorkFlowNodeUpdateError(BaseErrorCode):
    Code: int = 10531
    Msg: str = '<Node name>The feature has been upgraded and needs to be deleted and dragged back in.'


class WorkFlowVersionUpdateError(BaseErrorCode):
    Code: int = 10532
    Msg: str = 'The workflow version has been upgraded, please contact the creator to reschedule'


class WorkFlowTaskBusyError(BaseErrorCode):
    Code: int = 10540
    Msg: str = 'Server thread count is full, please try again later'


# Workflow Task Other Errors
class WorkFlowTaskOtherError(BaseErrorCode):
    Code: int = 10541
    Msg: str = 'Workflow task execution failed'


class AppWriteAuthError(BaseErrorCode):
    Code: int = 10599
    Msg: str = 'No Apply Write Permission'


# F027: cursor-based pagination — cursor parsing/version/context/key-length failed
class AppInvalidCursorError(BaseErrorCode):
    Code: int = 10550
    Msg: str = 'Invalid pagination cursor'


# F043: assistant node sql_agent schema inspection (module 105, segment 10560-10563)
class DbConnectionFailedError(BaseErrorCode):
    Code: int = 10560
    Msg: str = 'Database connection failed, please check the connection settings'


class DbDriverMissingError(BaseErrorCode):
    Code: int = 10561
    Msg: str = 'Missing database driver or client dependency, please check the environment'


class DbInspectTimeoutError(BaseErrorCode):
    Code: int = 10562
    Msg: str = 'Timed out while fetching metadata, please check the network and connection settings'


class DbSchemaNoValidTableError(BaseErrorCode):
    Code: int = 10563
    Msg: str = 'None of the selected tables exist in the database, please reselect'
