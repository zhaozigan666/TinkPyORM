"""tinkpyorm —— 参考 think-orm v4.0 设计、基于 Python 标准库的 SQLite ORM。

设计对齐（对应 think-orm 概念）:
    Db / Connection          -> 连接管理、事务
    Config / DatabaseManager -> 集中式配置与命名连接（v0.3.0 新增）
    Query / Builder          -> 查询构造器（链式操作、参数绑定）
    Model / Relation         -> ActiveRecord 模型、关联与预载入
    Collection / Paginator   -> 结果集、分页
    Raw / raw()              -> 原生表达式（Db::raw 等价物）

仅依赖 Python 标准库（sqlite3 / json / datetime / contextlib）。

推荐用法（配置只定义一次，任意模块共享）::

    import tinkpyorm
    tinkpyorm.configure({
        "default": {"database": "~/.app/vault.db", "prefix": "app_"},
        "log":     "sqlite:///~/.app/log.db",
    })
"""
from .collection import Collection, Paginator
from .config import Config, DEFAULT_ENV_PREFIX, load_file, parse_dsn
from .connection import Connection
from .db import Db
from .drivers import (
    Driver, SQLDriver, NoSQLDriver, SQLiteDriver,
    UnsupportedOperation, register_driver, get_driver, available_drivers,
)
from .exceptions import (
    OrmError, QueryError, DataNotFound, ModelNotFound,
    RelationNotFound, TransactionError, InvalidArgumentException,
    ConfigError, ConnectionNotFound, DriverNotAvailable,
)
from .manager import DatabaseManager, manager
from .model import Model
from .query import Query
from .relation import Relation
from .utils import Raw, raw, to_snake, to_camel

__version__ = "0.3.0"


def configure(databases=None, default=None, strict=None,
              env_prefix: str = DEFAULT_ENV_PREFIX) -> None:
    """注册数据库连接（推荐在应用启动时调用一次）。

    参数::

        databases: {"连接名": dict配置 | DSN字符串 | Config} 或单个 DSN/Config
        default:   默认连接名
        strict:    未注册连接时是否直接报错（默认 False，回退内存库并告警）
        env_prefix: 环境变量覆盖前缀
    """
    Db.configure(databases, default=default, strict=strict, env_prefix=env_prefix)


def connection(name=None) -> Connection:
    """取连接（懒建立 + 缓存），等价于 ``Db.get_connection(name)``。"""
    return Db.get_connection(name)


__all__ = [
    "Db", "Model", "Query", "Connection", "Collection", "Paginator", "Relation",
    "Raw", "raw", "to_snake", "to_camel",
    "Config", "DatabaseManager", "manager",
    "Driver", "SQLDriver", "NoSQLDriver", "SQLiteDriver",
    "register_driver", "get_driver", "available_drivers", "UnsupportedOperation",
    "configure", "connection", "load_file", "parse_dsn",
    "OrmError", "QueryError", "DataNotFound", "ModelNotFound",
    "RelationNotFound", "TransactionError", "InvalidArgumentException",
    "ConfigError", "ConnectionNotFound", "DriverNotAvailable",
]
