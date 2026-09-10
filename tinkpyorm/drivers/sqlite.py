"""SQLite 驱动（标准库 sqlite3）。

这是驱动抽象层的参考实现：新增其它数据库时，照此实现 :class:`Driver`
接口并在 :mod:`tinkpyorm.drivers` 注册即可。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterator, List, Optional, Sequence

from ..config import Config, SQLITE_CONNECT_KEYS
from ..exceptions import InvalidArgumentException
from .base import SQLDriver

#: 合法 journal_mode（对应 SQLite PRAGMA journal_mode）
JOURNAL_MODES = {"DELETE", "TRUNCATE", "PERSIST", "MEMORY", "WAL", "OFF"}


class SQLiteDriver(SQLDriver):
    """SQLite 驱动。

    配置选项（``Config.options``）::

        journal_mode: WAL / DELETE / TRUNCATE / PERSIST / MEMORY / OFF
        timeout / check_same_thread / isolation_level / uri / ... （透传 sqlite3.connect）
    """

    name = "sqlite"
    paramstyle = "qmark"

    def __init__(self, config: Config) -> None:
        super().__init__(config)
        self._conn: Optional[sqlite3.Connection] = None
        self.journal_mode = self._resolve_journal_mode()

    # ------------------------------------------------------------------ #
    # 连接
    # ------------------------------------------------------------------ #
    def _resolve_journal_mode(self) -> Optional[str]:
        raw = self.config.options.get("journal_mode")
        if raw is None:
            return None
        mode = str(raw).upper()
        if mode not in JOURNAL_MODES:
            raise InvalidArgumentException(
                f"非法 journal_mode: {raw!r}，可选 "
                + ", ".join(sorted(m.lower() for m in JOURNAL_MODES)))
        return mode

    def _connect_kwargs(self) -> Dict[str, Any]:
        kwargs = {k: v for k, v in self.config.options.items()
                  if k in SQLITE_CONNECT_KEYS}
        if self.config.connect_timeout is not None:
            kwargs.setdefault("timeout", self.config.connect_timeout)
        kwargs.setdefault("check_same_thread", False)
        return kwargs

    def connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.config.database,
                                         **self._connect_kwargs())
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            # WAL 等 journal_mode 仅对文件库有效，内存库自动跳过
            if self.journal_mode and self.config.database != ":memory:":
                self._conn.execute(f"PRAGMA journal_mode = {self.journal_mode}")
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def ping(self) -> bool:
        try:
            self.connect().execute("SELECT 1").close()
            return True
        except Exception:
            return False

    def is_connected(self) -> bool:
        return self._conn is not None

    @property
    def raw_connection(self) -> sqlite3.Connection:
        return self.connect()

    # ------------------------------------------------------------------ #
    # SQL 执行
    # ------------------------------------------------------------------ #
    def select(self, sql: str, params: Sequence[Any] = ()) -> List[dict]:
        cur = self.connect().execute(sql, tuple(params))
        try:
            rows = cur.fetchall()
        finally:
            cur.close()
        return [dict(r) for r in rows]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = self.connect().execute(sql, tuple(params))
        rowcount = cur.rowcount
        cur.close()
        return rowcount

    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = self.connect().execute(sql, tuple(params))
        lastrowid = cur.lastrowid
        cur.close()
        return lastrowid

    def select_stream(self, sql: str, params: Sequence[Any] = (),
                      chunk_size: int = 1000) -> Iterator[List[dict]]:
        """真流式查询：按 ``chunk_size`` 使用 ``fetchmany`` 逐块取数。

        相比 ``select()`` 的 ``fetchall()``，内存占用与结果集大小无关，
        适合全表扫描 / 大批量导出。
        """
        cur = self.connect().execute(sql, tuple(params))
        try:
            while True:
                batch = cur.fetchmany(chunk_size)
                if not batch:
                    break
                yield [dict(r) for r in batch]
        finally:
            cur.close()

    # ------------------------------------------------------------------ #
    # 事务
    # ------------------------------------------------------------------ #
    def begin(self) -> None:
        self.connect().execute("BEGIN")

    def commit(self) -> None:
        self.connect().commit()

    def rollback(self) -> None:
        self.connect().rollback()

    def savepoint(self, name: str) -> None:
        self.connect().execute(f"SAVEPOINT {name}")

    def release(self, name: str) -> None:
        self.connect().execute(f"RELEASE SAVEPOINT {name}")

    def rollback_to(self, name: str) -> None:
        self.connect().execute(f"ROLLBACK TO SAVEPOINT {name}")

    # ------------------------------------------------------------------ #
    # 方言
    # ------------------------------------------------------------------ #
    def table_exists(self, table: str) -> bool:
        rows = self.select(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,))
        return bool(rows)

    def table_fields(self, table: str) -> List[str]:
        rows = self.select(f"PRAGMA table_info({self.quote_identifier(table)})")
        return [r["name"] for r in rows]
