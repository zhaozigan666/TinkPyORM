"""thinkorm —— 参考 think-orm v4.0 设计、基于 Python 标准库的 SQLite ORM。

设计对齐（对应 think-orm 概念）:
    Db / Connection          -> 连接管理、事务
    Query / Builder          -> 查询构造器（链式操作、参数绑定）
    Model / Relation         -> ActiveRecord 模型、关联与预载入
    Collection / Paginator   -> 结果集、分页
    Raw / raw()              -> 原生表达式（Db::raw 等价物）

仅依赖 Python 标准库（sqlite3 / json / datetime / contextlib）。
"""
from .collection import Collection, Paginator
from .connection import Connection
from .db import Db
from .exceptions import (
    OrmError, QueryError, DataNotFound, ModelNotFound,
    RelationNotFound, TransactionError, InvalidArgumentException,
)
from .model import Model
from .query import Query
from .relation import Relation
from .utils import Raw, raw, to_snake, to_camel

__version__ = "0.1.1"
__all__ = [
    "Db", "Model", "Query", "Connection", "Collection", "Paginator", "Relation",
    "Raw", "raw", "to_snake", "to_camel",
    "OrmError", "QueryError", "DataNotFound", "ModelNotFound",
    "RelationNotFound", "TransactionError", "InvalidArgumentException",
]
