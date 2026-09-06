"""Db 门面（对应 think-orm 的 Db 静态类）。

提供全局连接管理、链式查询入口与原生 SQL 执行。

v0.3.0 起连接状态统一由 :mod:`tinkpyorm.manager` 管理，配置支持
dict / DSN / 环境变量 / 配置文件四种写法::

    import tinkpyorm
    tinkpyorm.configure({"default": "sqlite:///~/.app/vault.db"})

    Db.table('user').where('id', '>', 10).select()
    Db.connect('log').table('event').insert({...})   # 命名连接
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional, Sequence, Union

from .config import DEFAULT_ENV_PREFIX
from .connection import Connection
from .manager import manager
from .query import Query
from .utils import Raw, raw


class _DbProxy:
    """绑定指定连接名的门面代理，使 ``Db.connect('log').table(...)`` 成立。"""

    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    @property
    def connection_name(self) -> str:
        return self._name

    def table(self, table: str) -> Query:
        # 延迟解析连接与前缀：执行时才按连接名从注册中心取
        return Query(None, table=table, conn_name=self._name)

    def raw(self, expr: str) -> Raw:
        return raw(expr)

    def query(self, sql: str, params: Sequence[Any] = ()) -> List[dict]:
        return Db.get_connection(self._name).query(sql, params)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        return Db.get_connection(self._name).execute(sql, params)

    def transaction(self, callback: Optional[Callable[[], Any]] = None) -> Any:
        conn = Db.get_connection(self._name)
        if callback is not None:
            with conn.transaction():
                return callback()
        return conn.transaction()

    def connection(self) -> Connection:
        return Db.get_connection(self._name)

    def get_last_sql(self) -> str:
        return Db.get_connection(self._name).get_last_sql()


class Db:
    """数据库门面，用法与 think-orm 的 Db 类对齐::

        Db.table('user').where('id', '>', 10).select()
        Db.name('user')          # 自动加前缀
        Db.query('SELECT * FROM user WHERE id = ?', [1])
        Db.transaction(lambda: ...)   # 或 with Db.transaction():
        Db.connect('log').table('event').select()   # 命名连接
    """

    _default_name: str = "default"
    #: 兼容字段：未通过 Config 注册时（如直接传入 Connection）的表前缀
    _prefix: str = ""

    # ------------------------------------------------------------------ #
    # 配置与连接
    # ------------------------------------------------------------------ #
    @classmethod
    def configure(cls, databases: Optional[Any] = None,
                  default: Optional[str] = None,
                  strict: Optional[bool] = None,
                  env_prefix: str = DEFAULT_ENV_PREFIX) -> None:
        """批量注册连接（推荐入口，替代在每个模块里重复 set_config）。

        用法::

            Db.configure({
                "default": {"database": "~/.app/vault.db", "prefix": "app_"},
                "log":     "sqlite:///~/.app/log.db",
            })
        """
        manager.configure(databases, default=default, strict=strict,
                          env_prefix=env_prefix)
        if default is not None:
            cls._default_name = default

    @classmethod
    def set_config(cls, config: Union[dict, Connection, str],
                   name: str = "default") -> Connection:
        """设置数据库配置（保留以兼容旧代码，建议改用 ``configure``）。

        支持::

            Db.set_config({'database': 'app.db'})        # dict 配置
            Db.set_config(Connection('app.db'))          # 直接传入连接
            Db.set_config('app.db')                      # SQLite 文件路径

        与 v0.2.0 的差异（皆为修复性变更）：
        * 重复注册同名连接会先关闭旧连接，不再泄漏文件句柄；
        * 未知键归入驱动 options，不再透传给 sqlite3.connect 引发 TypeError；
        * 非法 type 立即报错，不再静默按 SQLite 处理。
        """
        if isinstance(config, Connection):
            manager.register_connection(name, config)
            if name == cls._default_name:
                manager.set_default(name)
            return config

        if isinstance(config, (str, dict)):
            cfg = manager.add(name, config)
            # 兼容旧语义：未显式给出 prefix 时保留既有值
            if cfg.prefix:
                cls._prefix = cfg.prefix
            if name == cls._default_name:
                manager.set_default(name)
            return manager.connection(name)

        raise TypeError(f"不支持的配置类型: {type(config)}")

    @classmethod
    def get_connection(cls, name: Optional[str] = None) -> Connection:
        """获取连接（模型层调用入口）。

        具名连接未注册时抛 ``ConnectionNotFound``（同时是 ConfigError 与
        KeyError 的子类，兼容旧版捕获 KeyError 的代码）。
        """
        return manager.connection(name if name is not None else cls._default_name)

    @classmethod
    def connection(cls, name: Optional[str] = None) -> Connection:
        """``get_connection`` 的语义别名。"""
        return cls.get_connection(name)

    @classmethod
    def connect(cls, name: str) -> _DbProxy:
        """绑定到指定命名连接的门面代理::

            Db.connect('log').table('event').insert({...})
        """
        return _DbProxy(name)

    @classmethod
    def configured(cls, name: Optional[str] = None) -> bool:
        """该连接名是否已配置（避免"未配置静默用内存库"的排查困境）。"""
        return manager.configured(name if name is not None else cls._default_name)

    @classmethod
    def close(cls, name: Optional[str] = None) -> None:
        """关闭连接（保留配置）。``name=None`` 时关闭全部。"""
        manager.disconnect(name)

    @classmethod
    def clear(cls) -> None:
        """关闭全部连接并清空配置（测试用）。"""
        manager.clear()

    @classmethod
    def _prefix_for(cls, name: Optional[str] = None) -> str:
        """取连接对应的表前缀（按连接隔离，修复旧版类级单值串味问题）。"""
        key = name if name is not None else cls._default_name
        cfg = manager.configs().get(key)
        return cfg.prefix if cfg is not None else cls._prefix

    # ------------------------------------------------------------------ #
    # 查询构造入口
    # ------------------------------------------------------------------ #
    @classmethod
    def table(cls, table: str) -> Query:
        """指定表名（含前缀）开始链式查询。

        连接与前缀均为延迟解析：``Db.table('user')`` 不立即建立连接，
        因此 configure() 可以在任意时刻调用（消除导入顺序依赖）。
        """
        return Query(None, table=table, conn_name=cls._default_name)

    @classmethod
    def name(cls, table: str) -> Query:
        """指定表名（自动补前缀）。"""
        return Query(None, conn_name=cls._default_name).name(table)

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
