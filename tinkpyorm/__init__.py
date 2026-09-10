"""tinkpyorm —— 参考 think-orm v4.0 设计、基于 Python 标准库的 SQLite ORM。

设计对齐（对应 think-orm 概念）:
    Db / Connection          -> 连接管理、事务
    Query / Builder          -> 查询构造器（链式操作、参数绑定）
    Model / Relation         -> ActiveRecord 模型、关联与预载入
    Collection / Paginator   -> 结果集、分页
    Raw / raw()              -> 原生表达式（Db::raw 等价物）
    drivers / Driver         -> 驱动抽象层（v0.3.0 引入，可扩展多数据库方言）
    cache / CacheStore       -> 查询缓存（v0.4.0 引入，进程级 + TTL + LRU）

仅依赖 Python 标准库（sqlite3 / json / datetime / contextlib / threading）。

推荐用法（任意模块调用一次 ``set_config``，全局共享）::

    from tinkpyorm import Db, Model

    Db.set_config({"database": "~/.app/vault.db", "prefix": "app_",
                   "journal_mode": "WAL"})
    Db.table("user").where("id", 1).find()

查询缓存与流式读取（v0.4.0）::

    Db.name("data").where({"url": url}).cache(10).find()   # 结果缓存 10 秒
    for batch in Db.table("log").order("id").chunk(500):   # 分块，线程安全
        ...
    for row in Db.table("log").cursor():                   # 真流式，内存恒定
        ...
"""
from .cache import CacheStore, MemoryCacheStore
from .collection import Collection, Paginator
from .connection import Connection
from .db import Db
from .drivers import (
    Driver, SQLDriver, NoSQLDriver, SQLiteDriver,
    UnsupportedOperation, register_driver, get_driver, available_drivers,
)
from .exceptions import (
    OrmError, QueryError, DataNotFound, ModelNotFound,
    RelationNotFound, TransactionError, InvalidArgumentException,
    DriverNotAvailable,
)
from .model import Model
from .query import Query
from .relation import Relation
from .utils import Raw, raw, to_snake, to_camel

__version__ = "0.4.0"


__all__ = [
    "Db", "Model", "Query", "Connection", "Collection", "Paginator", "Relation",
    "Raw", "raw", "to_snake", "to_camel",
    "CacheStore", "MemoryCacheStore",
    "Driver", "SQLDriver", "NoSQLDriver", "SQLiteDriver",
    "register_driver", "get_driver", "available_drivers", "UnsupportedOperation",
    "OrmError", "QueryError", "DataNotFound", "ModelNotFound",
    "RelationNotFound", "TransactionError", "InvalidArgumentException",
    "DriverNotAvailable",
]
