"""持久化层：四层留痕落库与查询。

`store` 负责表与读写，`tracker` 负责把调度事件翻译成记录，`schema` 存放表结构、
行对象与序列化口径。
"""

from .schema import RunRecordRow, definition_hash, json_dumps, json_loads
from .store import Store
from .tracker import RunTracker

__all__ = [
    "Store",
    "RunTracker",
    "RunRecordRow",
    "definition_hash",
    "json_dumps",
    "json_loads",
]
