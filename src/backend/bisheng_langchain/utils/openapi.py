import json
import re
from typing import Any

# 工作流节点的富文本编辑器会把行首缩进写成 U+00A0(NBSP) 等 Unicode 空格，
# 而 Python 的 json 只接受 空格/\t/\n/\r 作为空白，会直接抛 JSONDecodeError。
# 解析前先归一化这类字符，避免用户填写的 JSON 被当成普通字符串透传。
_UNICODE_SPACES = re.compile('[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000\ufeff]')


def convert_data_type_no_error(v: Any, data_type: Any) -> Any:
    try:
        return data_type(v)
    except Exception as e:
        return v


def loads_json_no_error(v: Any) -> Any:
    """解析JSON字符串，兼容富文本编辑器写入的 Unicode 空白字符。"""
    try:
        return json.loads(v)
    except json.JSONDecodeError:
        return json.loads(_UNICODE_SPACES.sub(' ', v))


def convert_openapi_field_value(v: Any, field_type: str) -> Any:
    """ Convert field value based on OpenAPI schema type."""
    v_type = type(v)
    if field_type == 'number' and v_type != float:
        return convert_data_type_no_error(v, float)
    elif field_type == 'integer' and v_type != int:
        return convert_data_type_no_error(v, int)
    elif field_type == 'boolean' and v_type != bool:
        return str(v).strip().lower() == 'true'
    elif field_type == 'string' and v_type != str:
        return convert_data_type_no_error(v, str)
    elif field_type == 'array' and v_type != list:
        return convert_data_type_no_error(v, loads_json_no_error)
    elif field_type in ['object', 'dict'] and v_type != dict:
        return convert_data_type_no_error(v, loads_json_no_error)
    return v
