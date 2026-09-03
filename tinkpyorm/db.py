"""Db 门面（对应 think-orm 的 Db 静态类）。

提供全局连接管理、链式查询入口与原生 SQL 执行。
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional, Sequence, Union

from .connection import Connection, get_default_connection, set_default_connection
from .query import Query
from .utils import Raw, raw


class Db:
    """数据库门面，用法与 think-orm 的 Db 类对齐::

        Db.table('user').where('id', '>', 10).select()
        Db.name('user')          # 自动加前缀
        Db.query('SELECT * FROM user WHERE id = ?', [1])
        Db.transaction(lambda: ...)   # 或 with Db.transaction():
    """

    _connections: Dict[str, Connection] = {}
    _default_name: str = "default"
    _prefix: str = ""

    # ------------------------------------------------------------------ #
    # 配置与连接
    # ------------------------------------------------------------------ #
    @classmethod
    def set_config(cls, config: Union[dict, Connection, str], name: str = "default") -> Connection:
        """设置数据库配置。

        支持::

            Db.set_config({'database': 'app.db'})        # dict 配置
            Db.set_config(Connection('app.db'))          # 直接传入连接
            Db.set_config('app.db')                      # SQLite 文件路径
        """
        if isinstance(config, Connection):
            conn = config
        elif isinstance(config, str):
            conn = Connection(config)
        elif isinstance(config, dict):
            database = config.get("database", config.get("dsn", ":memory:"))
            kwargs = {k: v for k, v in config.items() if k not in ("database", "dsn", "type", "prefix")}
            conn = Connection(database, **kwargs)
            cls._prefix = config.get("prefix", cls._prefix)
        else:
            raise TypeError(f"不支持的配置类型: {type(config)}")
        cls._connections[name] = conn
        if name == cls._default_name:
            set_default_connection(conn)
        return conn

    @classmethod
    def get_connection(cls, name: Optional[str] = None) -> Connection:
        """获取连接（模型层调用入口）。"""
        key = name if name is not None else cls._default_name
        if key in cls._connections:
            return cls._connections[key]
        if name is None or key == cls._default_name:
            return get_default_connection()
        raise KeyError(f"未配置数据库连接: {key}")

    @classmethod
    def close(cls, name: Optional[str] = None) -> None:
        if name is None:
            for conn in cls._connections.values():
                conn.close()
            get_default_connection().close()
            cls._connections.clear()
        elif name in cls._connections:
            cls._connections[name].close()
            del cls._connections[name]

    # ------------------------------------------------------------------ #
    # 查询构造入口
    # ------------------------------------------------------------------ #
    @classmethod
    def table(cls, table: str) -> Query:
        """指定表名（含前缀）开始链式查询。"""
        return Query(cls.get_connection(), table=table, prefix=cls._prefix)

    @classmethod
    def name(cls, table: str) -> Query:
        """指定表名（自动补前缀）。"""
        return Query(cls.get_connection(), prefix=cls._prefix).name(table)

    @classmethod
    def raw(cls, expr: str) -> Raw:
        """原生 SQL 表达式包装。"""
        return raw(expr)

    @classmethod
    def query(cls, sql: str, params: Sequence[Any] = ()) -> List[dict]:
        """原生查询（SELECT）。"""
        return cls.get_connection().query(sql, params)

    @classmethod
    def execute(cls, sql: str, params: Sequence[Any] = ()) -> int:
        """原生执行（写语句），返回影响行数。"""
        return cls.get_connection().execute(sql, params)

    # ------------------------------------------------------------------ #
    # 事务
    # ------------------------------------------------------------------ #
    @classmethod
    def transaction(cls, callback: Optional[Callable[[], Any]] = None) -> Any:
        """事务。两种用法::

            Db.transaction(lambda: (Db.execute(...), Db.execute(...)))  # 回调模式

            with Db.transaction():                                     # 上下文模式
                Db.execute(...)
        """
        conn = cls.get_connection()
        if callback is not None:
            with conn.transaction():
                return callback()
        return conn.transaction()

    # ------------------------------------------------------------------ #
    # 日志
    # ------------------------------------------------------------------ #
    @classmethod
    def get_sql_log(cls, name: Optional[str] = None) -> List[tuple]:
        return cls.get_connection(name).sql_log

    @classmethod
    def get_last_sql(cls, name: Optional[str] = None) -> str:
        return cls.get_connection(name).get_last_sql()

    @classmethod
    def sql_log_clear(cls, name: Optional[str] = None) -> None:
        cls.get_connection(name).sql_log_clear()

    @classmethod
    def sql_log_disable(cls, name: Optional[str] = None) -> None:
        """关闭 SQL 日志（生产环境推荐，避免长驻进程内存持续增长）。"""
        cls.get_connection(name).sql_log_disable()

    @classmethod
    def sql_log_enable(cls, max_size: Optional[int] = 1000,
                       name: Optional[str] = None) -> None:
        """开启 SQL 日志，最多保留 ``max_size`` 条（None 表示不限制）。"""
        cls.get_connection(name).sql_log_enable(max_size)
