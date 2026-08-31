"""SQLite 连接管理（基于标准库 sqlite3）。

对应 think-orm 的 Connection 层：负责连接建立/复用、SQL 执行、
参数绑定、事务（含 savepoint 嵌套）、SQL 日志。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from .exceptions import TransactionError


class Connection:
    """SQLite 连接封装。

    参数::

        Connection(':memory:')                  # 内存库
        Connection('app.db')                    # 文件库
        Connection('app.db', check_same_thread=False)  # 多线程共享
    """

    def __init__(self, database: str = ":memory:", **kwargs: Any):
        self.database = database
        self.kwargs = kwargs
        self._conn: Optional[sqlite3.Connection] = None
        self._in_transaction = False
        self._savepoint_depth = 0
        self.sql_log: List[Tuple[str, tuple]] = []

    # ------------------------------------------------------------------ #
    # 连接管理
    # ------------------------------------------------------------------ #
    @property
    def conn(self) -> sqlite3.Connection:
        """懒建立连接。"""
        if self._conn is None:
            kwargs = dict(self.kwargs)
            kwargs.setdefault("check_same_thread", False)
            self._conn = sqlite3.connect(self.database, **kwargs)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._in_transaction = False
        self._savepoint_depth = 0

    def table_exists(self, table: str) -> bool:
        row = self.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
        return bool(row)

    def table_fields(self, table: str) -> List[str]:
        """返回表的所有列名（缓存由调用方负责）。"""
        rows = self.query(f"PRAGMA table_info({_quote(table)})")
        return [r["name"] for r in rows]

    # ------------------------------------------------------------------ #
    # SQL 执行
    # ------------------------------------------------------------------ #
    def query(self, sql: str, params: Sequence[Any] = ()) -> List[dict]:
        """执行 SELECT 类语句，返回 dict 列表（无结果返回空列表）。"""
        cur = self.conn.execute(sql, tuple(params))
        try:
            rows = cur.fetchall()
        finally:
            cur.close()
        self.sql_log.append((sql, tuple(params)))
        return [dict(r) for r in rows]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行写语句（INSERT/UPDATE/DELETE/DDL），返回影响行数。

        非显式事务时自动提交（对应 think-orm 的自动提交行为）。
        """
        cur = self.conn.execute(sql, tuple(params))
        rowcount = cur.rowcount
        cur.close()
        self.sql_log.append((sql, tuple(params)))
        if not self._in_transaction:
            self.conn.commit()
        return rowcount

    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行 INSERT，返回 lastrowid（自增主键）。"""
        cur = self.conn.execute(sql, tuple(params))
        lastrowid = cur.lastrowid
        cur.close()
        self.sql_log.append((sql, tuple(params)))
        if not self._in_transaction:
            self.conn.commit()
        return lastrowid

    def get_last_sql(self) -> str:
        """最近一条 SQL（含参数占位），仅日志用途。"""
        if not self.sql_log:
            return ""
        sql, params = self.sql_log[-1]
        return self._render(sql, params)

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

        异常自动回滚，正常结束自动提交。
        """
        if self._in_transaction:
            self._savepoint_depth += 1
            sp = f"thinkorm_sp_{self._savepoint_depth}"
            self.conn.execute(f"SAVEPOINT {sp}")
            try:
                yield self
            except BaseException:
                self.conn.execute(f"ROLLBACK TO SAVEPOINT {sp}")
                raise
            finally:
                self.conn.execute(f"RELEASE SAVEPOINT {sp}")
                self._savepoint_depth -= 1
            return

        self.conn.execute("BEGIN")
        self._in_transaction = True
        try:
            yield self
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        finally:
            self._in_transaction = False
            self._savepoint_depth = 0

    def begin(self) -> None:
        if self._in_transaction:
            raise TransactionError("已有事务在进行中，请使用 savepoint 或嵌套事务")
        self.conn.execute("BEGIN")
        self._in_transaction = True

    def commit(self) -> None:
        if not self._in_transaction:
            raise TransactionError("当前没有进行中的事务")
        self.conn.commit()
        self._in_transaction = False

    def rollback(self) -> None:
        if not self._in_transaction:
            raise TransactionError("当前没有进行中的事务")
        self.conn.rollback()
        self._in_transaction = False

    @staticmethod
    def _render(sql: str, params: Sequence[Any]) -> str:
        """把参数内联进 SQL，仅供日志/调试展示（非执行）。"""
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


def _quote(ident: str) -> str:
    return f"`{ident.replace('`', '``')}`"


# 默认全局连接（Db 门面使用）
_default_connection: Optional[Connection] = None


def get_default_connection() -> Connection:
    global _default_connection
    if _default_connection is None:
        _default_connection = Connection()
    return _default_connection


def set_default_connection(conn: Connection) -> Connection:
    global _default_connection
    _default_connection = conn
    return conn
