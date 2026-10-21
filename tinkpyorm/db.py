"""Db 门面（对应 think-orm 的 Db 静态类）。

提供全局连接管理、链式查询入口与原生 SQL 执行。

v0.3.1 起配置入口收敛回简单的 ``Db.set_config``（撤销 v0.3.0 的集中式
配置机制）：在任意模块调用一次即可全局共享，无需注册表/DSN/环境变量::

    Db.set_config({'database': 'app.db', 'prefix': 'app_',
                   'journal_mode': 'WAL'})       # 一次配置，全局生效

    Db.table('user').where('id', '>', 10).select()
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence, Union

from .config import Config
from .connection import (
    Connection, get_default_connection, set_default_connection,
)
from .exceptions import InvalidArgumentException
from .query import Query
from .utils import Raw, raw

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查用
    from .document import DocumentCollection


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
    def set_config(cls, config: Union[dict, Connection, str, None] = None,
                   name: str = "default", **kwargs: Any) -> Connection:
        """设置数据库配置（一次配置、全局共享）。

        支持::

            Db.set_config({'database': 'app.db'})                 # dict 配置
            Db.set_config({'type': 'mysql', 'version': '8.0.36',  # 指定驱动与版本
                           'host': '127.0.0.1', 'database': 'app'})
            Db.set_config(type='mysql', version='8.0',            # 直接传参
                          host='127.0.0.1', database='app')
            Db.set_config(Connection('app.db'))                   # 直接传入连接
            Db.set_config('app.db')                               # SQLite 文件路径

        ``version`` 是 v0.11.0 起的一等配置键（仅 MySQL 消费，缺省按 5.7 处理，
        解析与校验见 :mod:`tinkpyorm.versioning`）。位置配置与关键字参数不可混用。

        修复性行为（相对 v0.2.0）：
        * 未知配置键归入驱动 options，不再透传底层驱动引发 TypeError；
        * 未注册的 ``type`` 立即报错，不再静默按 SQLite 处理。合法性以活驱动
          注册表为准，故 ``register_driver("oracle", OracleDriver)`` 之后
          ``{'type': 'oracle'}`` 即为合法配置（v0.7.1 起）。
        """
        if kwargs:
            if config is not None:
                raise InvalidArgumentException(
                    "set_config 的位置配置与关键字参数不能混用")
            config = kwargs
        if config is None:
            raise InvalidArgumentException("set_config 需要配置字典/连接/路径")

        if isinstance(config, Connection):
            conn = config
            cls._connections[name] = conn
        elif isinstance(config, str):
            conn = Connection(config)
            cls._connections[name] = conn
        elif isinstance(config, dict):
            cfg = Config.from_dict(config)
            conn = Connection.from_config(cfg)
            prefix = cfg.prefix
            if prefix:
                cls._prefix = prefix
            cls._connections[name] = conn
            cls._apply_cache_config(config)
        else:
            raise TypeError(f"不支持的配置类型: {type(config)}")
        if name == cls._default_name:
            set_default_connection(conn)
        return conn

    @classmethod
    def config(cls, config: Union[dict, Connection, str, None] = None,
               name: str = "default", **kwargs: Any) -> Connection:
        """``set_config`` 的别名（v0.11.0）。

        保留两个名字是因为版本号等新配置项常被写在
        ``Db.config(type="mysql", version="8.0.36", ...)`` 这种"配置"语义
        的调用里；两者完全等价，共享同一份连接注册表。
        """
        return cls.set_config(config, name=name, **kwargs)

    @classmethod
    def _apply_cache_config(cls, config: dict) -> None:
        """按 ``set_config`` 传入的缓存键切换缓存后端（v0.9.1）。

        仅当本次显式传入 ``cache_backend`` / ``cache_dir`` 时才切换，
        不打扰手动 ``cache.set_store()`` 注入的自定义后端：

        * ``cache_backend="file"`` → 启用 :class:`FileCacheStore`
          （目录已存在同路径实例时复用，避免重复清空）；
        * ``cache_backend="memory"`` → 若当前是文件后端则切回内存，
          其他自定义后端保持不变。
        """
        if not isinstance(config, dict) or not (
                "cache_backend" in config or "cache_dir" in config):
            return
        from . import cache as _cache
        from .cache import FileCacheStore
        cfg = cls.get_connection().config
        if cfg.cache_backend == "file":
            current = _cache.store()
            if isinstance(current, FileCacheStore) and \
                    current.cache_dir == cfg.cache_dir:
                return
            _cache.set_store(FileCacheStore(cfg.cache_dir))
        elif isinstance(_cache.store(), FileCacheStore):
            _cache.set_store(None)  # 切回内存后端

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
        """关闭连接。``name=None`` 时关闭全部并清空注册。"""
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
    def collection(cls, name: str) -> "DocumentCollection":
        """打开文档集合（schemaless 文档存储，v0.8.0）。

        无需提前建表与声明字段，首次访问自动建表；用法见
        :class:`~tinkpyorm.document.DocumentCollection`::

            Db.collection("users").insert({"name": "张三", "age": 25})
            Db.collection("users").where("age", ">=", 18).select()
        """
        from .document import DocumentCollection
        return DocumentCollection(cls.get_connection(), name, prefix=cls._prefix)

    @classmethod
    def name(cls, table: str) -> Query:
        """指定表名（自动补前缀）。"""
        return Query(cls.get_connection(), prefix=cls._prefix).name(table)

    @classmethod
    def mongo(cls, name: str, *,
              write_concern: Optional[Dict[str, Any]] = None) -> "MongoCollection":
        """打开 MongoDB 集合（v0.12.0）。

        仅在 ``type="mongodb"`` 的连接下可用；其它驱动在构造期即抛
        :class:`UnsupportedOperation`（与 :meth:`collection` 的行为一致）::

            Db.config({"type": "mongodb", "database": "app",
                       "options": {"uri": "mongodb://127.0.0.1:27017"}})

            users = Db.mongo("users")
            users.insert({"name": "张三", "age": 25})
            users.where("age", ">=", 18).order("age", "desc").limit(10).select()

        ``write_concern`` 可按集合覆盖写关注（如 ``{"w": "majority"}``）。
        详见 :class:`~tinkpyorm.mongo.MongoCollection`。
        """
        from .mongo import MongoCollection
        return MongoCollection(cls.get_connection(), name, prefix=cls._prefix,
                               write_concern=write_concern)

    @classmethod
    def fts(cls, name: str, columns: Any = None, *,
            tokenize: str = "trigram", prefix: Any = None,
            standalone: bool = False, content_rowid: Optional[str] = None,
            model: Optional[type] = None) -> "FtsIndex":
        """打开全文检索索引（SQLite FTS5，v0.10.0）。

        默认 external 模式：索引只存词条，原文留在主表，create() 会自动
        挂同步触发器；``standalone=True`` 则建独立 FTS 表（需显式给列）::

            docs = Db.fts("docs")                       # 列自动从 docs 表推断
            docs.create()                               # 幂等建表 + 触发器
            docs.search("sqlite OR 索引").rank().limit(10).select()

            notes = Db.fts("notes", ["title", "body"], standalone=True)
            notes.create()

        分词器默认 ``trigram``（中英文兼顾的子串检索，查询串需 ≥3 字符）。
        详见 :class:`~tinkpyorm.fts.FtsIndex`。
        """
        from .fts import FtsIndex
        return FtsIndex(cls.get_connection(), cls._prefix + name,
                        columns, tokenize=tokenize, prefix=prefix,
                        standalone=standalone, content_rowid=content_rowid,
                        model=model)

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

    # ------------------------------------------------------------------ #
    # 查询缓存
    # ------------------------------------------------------------------ #
    @classmethod
    def clear_cache(cls, prefix: Optional[str] = None) -> int:
        """清空查询缓存，返回清除条数。

        链式查询的写操作（insert / update / delete 等）会自动清除对应表的
        缓存；但裸 SQL（``Db.execute('UPDATE ...')``）不会，此时需手动调用
        本方法，避免读到过期结果。
        """
        from . import cache as _cache
        return _cache.clear(prefix)

    @classmethod
    def cache_store(cls) -> Any:
        """返回当前缓存后端（可替换为自定义实现，详见 ``tinkpyorm.cache``）。"""
        from . import cache as _cache
        return _cache.store()
