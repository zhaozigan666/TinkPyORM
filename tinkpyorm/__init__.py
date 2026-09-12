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

JSON 查询与自动格式化（v0.5.0）::

    Db.table("user").json().find(1)                        # 结果自动解析为 dict
    Db.table("user").where_json("extra", "$.age", ">", 18).select()
    Db.table("user").where_json_contains("extra", "$.tags", "vip").select()
    Db.table("user").field_json("extra", "$.city", "city").select()
    Db.table("user").order_json("extra", "$.score", "desc").select()

写库时 ``dict`` / ``list`` 自动序列化为 JSON 文本，读库时自动还原为
Python 对象；JSON 条件的 SQL 形态由驱动提供，可扩展至 MySQL / PostgreSQL
（MongoDB / Redis 走原生对象，无需 SQL 层）。

JSON 路径级局部更新（v0.6.0）::

    Db.table("user").where("id", 1).update_json("extra", "$.age", 19)
    Db.table("user").where("id", 1).update_json(
        "extra", {"$.age": 19, "$.city": "上海"})       # 一次 SQL 原子写入
    Db.table("user").where("id", 1).update_json_insert("extra", "$.level", "g")
    Db.table("user").where("id", 1).update_json_remove("extra", ["$.tmp", "$.cache"])
    Db.table("user").where("id", 1).update_json_patch("extra", {"tmp": None})

一条语句内混合多种操作（v0.7.0）::

    Db.table("user").where("id", 1).update_json_ops("extra", [
        ("set",    "$.age", 19),        # 改值
        ("remove", "$.tmp"),            # 删键
        ("insert", "$.level", "vip"),   # 不存在才写
        ("patch",  {"meta": {"v": 2}}), # RFC 7396 合并
        ("remove", ["$.a", "$.b"]),     # 一次删多个
    ])

在 SQL 内改写嵌套字段，没有"读出 → 改 → 写回"窗口，故并发交错时不会
丢失更新；值与路径均经校验，值一律参数绑定。
"""
from .builder import JsonWhere, JsonUpdate, JSON_OPS, JSON_UPDATE_MODES
from .cache import CacheStore, MemoryCacheStore
from .collection import Collection, Paginator
from .connection import Connection
from .db import Db
from .drivers import (
    Driver, SQLDriver, NoSQLDriver, SQLiteDriver,
    UnsupportedOperation, register_driver, get_driver, available_drivers,
    normalize_json_path,
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

__version__ = "0.7.0"


__all__ = [
    "Db", "Model", "Query", "Connection", "Collection", "Paginator", "Relation",
    "Raw", "raw", "to_snake", "to_camel",
    "CacheStore", "MemoryCacheStore",
    "JsonWhere", "JsonUpdate", "JSON_OPS", "JSON_UPDATE_MODES",
    "normalize_json_path",
    "Driver", "SQLDriver", "NoSQLDriver", "SQLiteDriver",
    "register_driver", "get_driver", "available_drivers", "UnsupportedOperation",
    "OrmError", "QueryError", "DataNotFound", "ModelNotFound",
    "RelationNotFound", "TransactionError", "InvalidArgumentException",
    "DriverNotAvailable",
]
