"""数据库连接门面（语义层）。

职责：SQL 日志、事务状态机、自增主键 / 影响行数的语义封装、跨线程保护。
具体执行与 SQL 方言差异全部委派给 :mod:`tinkpyorm.drivers` 中的驱动实现，
因此新增数据库类型时本文件无需改动。

参数::

    Connection(':memory:')                  # 内存库
    Connection('app.db')                    # 文件库（默认开启线程保护）
    Connection('app.db', journal_mode='WAL')
    Connection('app.db', thread_safe=False) # 关闭内部锁（单线程可省开销）

线程安全说明：SQLite 驱动默认以 ``check_same_thread=False`` 建立连接，
允许多线程共享；但 ``_in_transaction`` / ``_savepoint_depth`` 等事务状态
是共享可变状态，若不加保护，一个线程的 ``commit`` 可能提交另一个线程
未完成的事务。本类内部使用可重入锁（``RLock``）串行化所有 SQL 执行与
事务操作，保证事务语义正确；代价是事务期间独占连接（嵌套调用不受影响，
因为 RLock 可重入）。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .config import Config, SQLITE_CONNECT_KEYS
from .drivers import Driver, get_driver
from .exceptions import TransactionError

# SQL 日志默认保留条数上限（环形裁剪）。长驻进程若不限制，
# 每条 SQL 约 220 字节，百万次操作将累积数百 MB 且永不释放。
DEFAULT_SQL_LOG_MAX = 1000


class Connection:
    """数据库连接（内部持有 :class:`Config` 与 :class:`Driver`）。"""

    def __init__(self, database: str = ":memory:",
                 config: Optional[Config] = None,
                 thread_safe: bool = True, **kwargs: Any):
        if config is None:
            # 兼容旧式构造：位置/关键字参数转换为等价的配置对象
            sql_log_enabled = bool(kwargs.pop("sql_log_enabled", True))
            sql_log_max = kwargs.pop("sql_log_max", DEFAULT_SQL_LOG_MAX)
            journal_mode = kwargs.pop("journal_mode", None)
            options = dict(kwargs)
            if journal_mode is not None:
                options["journal_mode"] = journal_mode
            config = Config(
                database=database,
                type="sqlite",
                options=options,
                sql_log_enabled=sql_log_enabled,
                sql_log_max=sql_log_max,
            )
        self.config: Config = config
        # 驱动实例化时校验驱动专属参数（如 SQLite 的 journal_mode）
        self.driver: Driver = get_driver(config.type)(config)
        self._in_transaction = False
        self._savepoint_depth = 0
        self.sql_log: List[Tuple[str, tuple]] = []
        # 可重入锁：保护 SQL 执行与事务状态（见模块 docstring）
        self._thread_safe = bool(thread_safe)
        self._lock: Optional[threading.RLock] = \
            threading.RLock() if self._thread_safe else None

    @contextmanager
    def _guard(self) -> Iterator[None]:
        """线程保护哨兵；``thread_safe=False`` 时零开销透传。"""
        if self._lock is None:
            yield
        else:
            with self._lock:
                yield

    # ------------------------------------------------------------------ #
    # 构造入口
    # ------------------------------------------------------------------ #
    @classmethod
    def from_config(cls, config: Config) -> "Connection":
        """由配置对象建立连接（配置层的统一构造入口）。

        配置中非通用键已归入 ``config.options``，由驱动按白名单取用，
        避免未知参数透传给底层驱动。
        """
        # 先解析驱动，给出"依赖未安装"等明确提示
        get_driver(config.type)
        return cls(config=config)

    # ------------------------------------------------------------------ #
    # 兼容属性（v0.2.0 既有访问方式）
    # ------------------------------------------------------------------ #
    @property
    def database(self) -> str:
        return self.config.database

    @database.setter
    def database(self, value: str) -> None:
        self.config.database = value

    @property
    def journal_mode(self) -> Optional[str]:
        """当前 journal_mode（SQLite 专有，其它驱动为 None）。"""
        return getattr(self.driver, "journal_mode", None)

    @property
    def kwargs(self) -> Dict[str, Any]:
        """底层驱动的透传参数（SQLite 为 sqlite3.connect 参数）。"""
        return {k: v for k, v in self.config.options.items()
                if k in SQLITE_CONNECT_KEYS}

    @property
    def conn(self) -> Any:
        """底层原生连接（懒建立）。"""
        return self.driver.raw_connection

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #
    def close(self) -> None:
        with self._guard():
            self.driver.close()
            self._in_transaction = False
            self._savepoint_depth = 0

    def ping(self) -> bool:
        """连接是否可用。"""
        return self.driver.ping()

    @property
    def connected(self) -> bool:
        """底层连接是否已建立（未连接 / 已关闭均为 False）。"""
        return self.driver.is_connected()

    def table_exists(self, table: str) -> bool:
        return self.driver.table_exists(table)

    def table_fields(self, table: str) -> List[str]:
        """返回表的所有列名（缓存由调用方负责）。"""
        return self.driver.table_fields(table)

    # ------------------------------------------------------------------ #
    # SQL 执行
    # ------------------------------------------------------------------ #
    def query(self, sql: str, params: Sequence[Any] = ()) -> List[dict]:
        """执行 SELECT 类语句，返回 dict 列表（无结果返回空列表）。"""
        with self._guard():
            rows = self.driver.select(sql, params)
            self._log(sql, params)
        return rows

    def query_stream(self, sql: str, params: Sequence[Any] = (),
                     chunk_size: int = 1000) -> Iterator[dict]:
        """流式执行 SELECT，逐行产出 dict（大结果集不整体载入内存）。

        实现要点：在锁内启动游标（首块数据同时取出），随后**释放锁**继续
        迭代——流式读取期间只读且不修改事务状态，无需长期独占连接；
        因此本方法不适合与同一连接上的并发写入同时使用。
        """
        with self._guard():
            stream = self.driver.select_stream(sql, params, chunk_size)
            first = next(iter(stream), None)
            self._log(sql, params)
        if not first:
            return
        for row in first:
            yield row
        for batch in stream:
            for row in batch:
                yield row

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行写语句（INSERT/UPDATE/DELETE/DDL），返回影响行数。

        非显式事务时自动提交（对应 think-orm 的自动提交行为）。
        """
        with self._guard():
            rowcount = self.driver.execute(sql, params)
            self._log(sql, params)
            if not self._in_transaction:
                self.driver.commit()
        return rowcount

    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行 INSERT，返回 lastrowid（自增主键）。"""
        with self._guard():
            lastrowid = self.driver.insert(sql, params)
            self._log(sql, params)
            if not self._in_transaction:
                self.driver.commit()
        return lastrowid

    def _log(self, sql: str, params: Sequence[Any]) -> None:
        """记录 SQL 日志，受开关与上限约束（关闭时零开销）。"""
        if not self.config.sql_log_enabled:
            return
        log = self.sql_log
        log.append((sql, tuple(params)))
        max_size = self.config.sql_log_max
        if max_size is not None and len(log) > max_size:
            del log[: len(log) - max_size]

    def sql_log_disable(self) -> None:
        """关闭 SQL 日志（生产环境推荐）：省去记录开销并释放已占内存。"""
        self.config.sql_log_enabled = False
        self.sql_log.clear()

    def sql_log_enable(self, max_size: Optional[int] = DEFAULT_SQL_LOG_MAX) -> None:
        """开启 SQL 日志。``max_size`` 为保留条数上限，None 表示不限制。"""
        self.config.sql_log_enabled = True
        self.config.sql_log_max = max_size

    def get_last_sql(self) -> str:
        """最近一条 SQL（含参数占位），仅日志用途。"""
        if not self.sql_log:
            return ""
        sql, params = self.sql_log[-1]
        return self.driver.render_sql(sql, params)

    def sql_log_clear(self) -> None:
        self.sql_log.clear()

    # ------------------------------------------------------------------ #
    # 事务
    # ------------------------------------------------------------------ #
    @contextmanager
    def transaction(self) -> Iterator["Connection"]:
        """事务上下文管理器，支持嵌套（内层使用 SAVEPOINT）。

        用法::

            with db.transaction():
                db.execute(...)
                ...

        异常自动回滚，正常结束自动提交。整个事务期间持有连接锁（可重入），
        其它线程的 SQL 会排队等待，从而避免事务状态被交叉污染。
        """
        with self._guard():
            if self._in_transaction:
                self._savepoint_depth += 1
                sp = f"tinkpyorm_sp_{self._savepoint_depth}"
                self.driver.savepoint(sp)
                try:
                    yield self
                except BaseException:
                    self.driver.rollback_to(sp)
                    raise
                finally:
                    self.driver.release(sp)
                    self._savepoint_depth -= 1
                return

            self.driver.begin()
            self._in_transaction = True
            try:
                yield self
                self.driver.commit()
            except BaseException:
                self.driver.rollback()
                raise
            finally:
                self._in_transaction = False
                self._savepoint_depth = 0

    def begin(self) -> None:
        with self._guard():
            if self._in_transaction:
                raise TransactionError("已有事务在进行中，请使用 savepoint 或嵌套事务")
            self.driver.begin()
            self._in_transaction = True

    def commit(self) -> None:
        with self._guard():
            if not self._in_transaction:
                raise TransactionError("当前没有进行中的事务")
            self.driver.commit()
            self._in_transaction = False

    def rollback(self) -> None:
        with self._guard():
            if not self._in_transaction:
                raise TransactionError("当前没有进行中的事务")
            self.driver.rollback()
            self._in_transaction = False

    @staticmethod
    def _render(sql: str, params: Sequence[Any]) -> str:
        """把参数内联进 SQL，仅供日志/调试展示（非执行）。

        保留为静态方法以兼容旧调用；实际日志渲染由驱动的 ``render_sql``
        完成，以便适配不同占位符风格（? / %s / :1）。
        """
        out, idx = [], 0
        for ch in sql:
            if ch == "?" and idx < len(params):
                p = params[idx]
                idx += 1
                if p is None:
                    out.append("NULL")
                elif isinstance(p, (int, float)):
                    out.append(str(p))
                else:
                    out.append("'" + str(p).replace("'", "''") + "'")
            else:
                out.append(ch)
        return "".join(out)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Connection {self.config.type}:{self.config.database}>"


# 默认全局连接（Db 门面 set_config(name="default") 时同步写入）
_default_connection: Optional["Connection"] = None


def get_default_connection() -> "Connection":
    """返回默认连接；未设置时自动建立一个匿名内存库（兼容旧行为）。"""
    global _default_connection
    if _default_connection is None:
        _default_connection = Connection()
    return _default_connection


def set_default_connection(conn: "Connection") -> "Connection":
    """把连接登记为全局默认连接。"""
    global _default_connection
    _default_connection = conn
    return conn
