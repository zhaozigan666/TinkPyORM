"""SQLite 驱动（标准库 sqlite3）。

这是驱动抽象层的参考实现：新增其它数据库时，照此实现 :class:`Driver`
接口并在 :mod:`tinkpyorm.drivers` 注册即可。
"""
from __future__ import annotations

import sqlite3
from typing import Any, Dict, Iterator, List, Optional, Sequence

from ..config import Config, SQLITE_CONNECT_KEYS
from ..exceptions import InvalidArgumentException
from .base import SQLDriver, json_path_literal

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

    # ------------------------------------------------------------------ #
    # JSON 能力（SQLite JSON1，3.38+ 默认内置；本项目按 3.53 实现）
    # ------------------------------------------------------------------ #
    # 参考实现说明（扩展其它数据库时对照覆写本组方法即可）：
    #   MySQL    : JSON_EXTRACT(col, path) 同名同参，JSON_CONTAINS 参数顺序
    #              为 (col, 待查找值, path)，JSON_LENGTH(col, path)
    #   Postgres : col #> '{a,b}' / jsonb_array_length(col #> path)
    supports_json = True

    def json_extract(self, column_sql: str, path: Any) -> str:
        """``json_extract(col, '$.a.b')``。

        路径经 :func:`normalize_json_path` 校验后内联为字面量；
        返回值为 SQLite 原生类型（数字/文本/null），比较运算可直接使用。
        """
        return f"json_extract({column_sql}, {json_path_literal(path)})"

    def json_exists(self, column_sql: str, path: Any) -> str:
        """路径存在性判断。

        用 ``json_type`` 而非 ``json_extract IS NOT NULL``，因为后者无法
        区分"路径不存在"与"路径值为 JSON null"。
        """
        return f"(json_type({column_sql}, {json_path_literal(path)}) IS NOT NULL)"

    def json_contains(self, column_sql: str, path: Any,
                      placeholder: str = "?") -> str:
        """数组元素包含判断。

        以表值函数 ``json_each`` 展开数组后逐元素比较，因此对
        ``$.tags`` 这类路径可正确命中，且绑定参数由 Builder 提供::

            EXISTS (SELECT 1 FROM json_each(col, '$.tags') WHERE value = ?)
        """
        return (f"(EXISTS (SELECT 1 FROM json_each({column_sql}, "
                f"{json_path_literal(path)}) WHERE value = {placeholder}))")

    def json_length(self, column_sql: str, path: Any) -> str:
        """元素个数（数组与对象通用）。

        用 ``json_each`` 计数而非 ``json_array_length``，以便同时支持
        对象类型；路径不存在时结果为 0（与空数组无法区分）。
        """
        return (f"(SELECT count(*) FROM json_each({column_sql}, "
                f"{json_path_literal(path)}))")

    def json_type(self, column_sql: str, path: Any) -> str:
        """JSON 类型名（``null``/``true``/``false``/``integer``/``real``/
        ``text``/``array``/``object``）；路径不存在返回 SQL NULL。"""
        return f"json_type({column_sql}, {json_path_literal(path)})"

    # ------------------------------------------------------------------ #
    # JSON 路径写入（局部更新，JSON1 提供）
    # ------------------------------------------------------------------ #
    @staticmethod
    def _json_base(column_sql: str, ifnull: Any) -> str:
        """构造"空列兜底"后的基表达式。

        ``json_set(NULL, ...)`` 在 SQLite 中返回 NULL——即对尚未写入过
        JSON 的列做路径更新会**静默无效**。显式传入空文档时以 ``COALESCE``
        兜底；空文档是驱动常量字面量（``'{}'`` / ``'[]'``），不引入绑定
        参数，也就没有注入面。
        """
        if ifnull is None:
            return column_sql
        if isinstance(ifnull, dict) and not ifnull:
            return f"COALESCE({column_sql}, '{{}}')"
        if isinstance(ifnull, (list, tuple)) and not ifnull:
            return f"COALESCE({column_sql}, '[]')"
        raise InvalidArgumentException(
            "ifnull 仅接受空 dict（视为空对象）或空 list（视为空数组）")

    def json_set(self, column_sql: str, pairs: Sequence[Any],
                 ifnull: Any = None) -> str:
        """``json_set(col, '$.a', ?, '$.b', ?)``。

        多组路径/值编译进**同一次调用**：SQLite 允许 ``json_set`` 接受
        任意多组参数，且该调用内部按顺序生效；若拆成同一列的两个 SET
        子句，则只有最后一个生效（前者静默丢失）。
        """
        pairs = list(pairs)
        if not pairs:
            raise InvalidArgumentException("json_set 至少需要一组路径与值")
        base = self._json_base(column_sql, ifnull)
        args = ", ".join(f"{path}, {value_sql}" for path, value_sql in pairs)
        return f"json_set({base}, {args})"

    def json_insert(self, column_sql: str, pairs: Sequence[Any],
                    ifnull: Any = None) -> str:
        """``json_insert(col, '$.a', ?)`` —— 仅当路径不存在时写入。"""
        pairs = list(pairs)
        if not pairs:
            raise InvalidArgumentException("json_insert 至少需要一组路径与值")
        base = self._json_base(column_sql, ifnull)
        args = ", ".join(f"{path}, {value_sql}" for path, value_sql in pairs)
        return f"json_insert({base}, {args})"

    def json_remove(self, column_sql: str, paths: Sequence[str],
                    ifnull: Any = None) -> str:
        """``json_remove(col, '$.a', '$.b')`` —— 删除路径，可多个。

        路径不存在时静默忽略；多路径合并为一次调用，理由同
        :meth:`json_set`（避免同列多 SET 子句互相覆盖）。
        """
        paths = list(paths)
        if not paths:
            raise InvalidArgumentException("json_remove 至少需要一个路径")
        base = self._json_base(column_sql, ifnull)
        return f"json_remove({base}, {', '.join(paths)})"

    def json_patch(self, column_sql: str, value_sql: str,
                   ifnull: Any = None) -> str:
        """``json_patch(col, ?)`` —— RFC 7396 合并补丁。

        SQLite 的 ``json_patch`` 会自行把第二个参数解析为 JSON，因此
        ``json(?)`` 包装与裸文本绑定均可（见 test_json.py 语义用例）。
        """
        base = self._json_base(column_sql, ifnull)
        return f"json_patch({base}, {value_sql})"

    def json_bind(self, placeholder: str, value: Any) -> str:
        """容器与布尔值需以 ``json(?)`` 包装。

        若直接绑定文本参数，SQLite 会把嵌套对象存成**转义字符串**
        （``{"n":"{\\"k\\":1}"}``）而非嵌套对象；``json(?)`` 让参数按
        JSON 值解析。

        布尔同样需要包装：直接绑定 ``True`` 会被 sqlite3 适配为整数 1，
        落库成 ``1`` 而不是 JSON ``true``（``json_type`` 会报 ``integer``，
        下游消费方看到的是数字）。包装后按 JSON 文本 ``'true'`` /
        ``'false'`` 解析，保持类型保真。

        标量（数字 / 文本 / ``None``）无需包装：json_set 会把 TEXT 参数
        当 JSON 字符串、NULL 当 JSON null，语义已正确。
        """
        if isinstance(value, (dict, list, tuple, bool)):
            return f"json({placeholder})"
        return placeholder
