"""MySQL 驱动（5.7 / 8.x / 9.x，v0.11.0）。

依赖第三方客户端 ``pymysql``（可选依赖）：未安装时 ``get_driver("mysql")``
会给出 ``pip install tinkpyorm[mysql]`` 的提示，不影响 SQLite 用户。

设计要点
========

1. **版本是一等配置**：``Db.config(type="mysql", version="8.0.36", ...)`` 或
   ``Db.config({"type": "mysql", "version": "8.0.36", ...})``。缺省按
   ``5.7.0`` 处理（见 :mod:`tinkpyorm.versioning`）。
2. **能力矩阵而非 if 散落**：所有版本差异集中在 :data:`MYSQL_FEATURES`
   （最低版本）与 :data:`MYSQL_REMOVED`（移除版本）。上层通过
   ``driver.supports_feature("cte")`` / ``driver.require_feature("cte")``
   询问，不在 Query/Builder 里写版本号。
3. **降级或报错，不静默**：
   - 能用等价写法的地方做**降级**（upsert 的 ``VALUES(col)`` vs 行别名）；
   - 无等价写法的地方**报错**（5.7 无 CTE、无 SKIP LOCKED、无 EXPLAIN ANALYZE），
     错误信息带"需要 >= x，当前 y"。
4. **JSON 方言**：MySQL 的 ``JSON_TYPE`` 返回大写且把布尔叫 ``BOOLEAN``，
   与 SQLite 的 ``text`` / ``true`` / ``false`` 词汇不一致，故在 SQL 层用
   ``CASE`` 归一，保证 ``where_json_type`` 的取值跨库一致。
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator, List, Optional, Sequence

from ..exceptions import DriverNotAvailable, InvalidArgumentException, QueryError
from ..versioning import MYSQL_SUPPORTED, Version, parse_version
from .base import SQLDriver, json_path_literal

__all__ = ["MySQLDriver", "MYSQL_FEATURES", "MYSQL_REMOVED"]

#: 功能名 -> 最低支持版本。未列出的功能视为"全版本支持"。
MYSQL_FEATURES: Dict[str, Version] = {
    # 5.7 线
    "json": Version(5, 7, 8),
    "json_merge_patch": Version(5, 7, 22),
    "generated_columns": Version(5, 7, 6),
    "ngram_parser": Version(5, 7, 6),
    "native_password_auth": Version(5, 0, 0),
    # 8.0 线
    "cte": Version(8, 0, 1),
    "nowait": Version(8, 0, 1),
    "skip_locked": Version(8, 0, 1),
    "window_functions": Version(8, 0, 2),
    "json_table": Version(8, 0, 4),
    "functional_index": Version(8, 0, 13),
    "default_expr": Version(8, 0, 13),
    "check_constraint": Version(8, 0, 16),
    "multi_valued_index": Version(8, 0, 17),
    "values_alias": Version(8, 0, 19),
    "invisible_index": Version(8, 0, 23),
    "except_intersect": Version(8, 0, 31),
}

#: 功能名 -> 被移除的版本（``version >= 该版本`` 即不可用）。
MYSQL_REMOVED: Dict[str, Version] = {
    # 9.0.0 移除服务端 mysql_native_password 插件
    "native_password_auth": Version(9, 0, 0),
}

#: 可直接透传给 pymysql.connect() 的驱动专属参数
MYSQL_CONNECT_KEYS = frozenset({
    "charset", "collation", "unix_socket", "ssl", "init_command",
    "read_timeout", "write_timeout", "client_flag", "program_name",
})

#: 5.7 默认字符集是 latin1，8.0 起默认 utf8mb4；统一显式指定，避免跨版本乱码
DEFAULT_CHARSET = "utf8mb4"

#: 各版本可用的默认排序规则
DEFAULT_COLLATIONS = {
    "5.7": "utf8mb4_unicode_ci",
    "8": "utf8mb4_0900_ai_ci",
    "9": "utf8mb4_0900_ai_ci",
}


class MySQLDriver(SQLDriver):
    """MySQL 驱动（SQL 方言 + pymysql 连接）。"""

    name = "mysql"
    paramstyle = "format"
    supports_transactions = True
    supports_json = True
    supports_document = False   # 文档集合依赖 SQLite 专有能力
    supports_fts = False        # MySQL 用 FULLTEXT + MATCH...AGAINST，另一套语义
    supports_upsert = True      # INSERT ... ON DUPLICATE KEY UPDATE

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        self.version: Version = parse_version(
            getattr(config, "version", None), product="mysql")
        self._conn: Any = None

    # ------------------------------------------------------------------ #
    # 版本与能力
    # ------------------------------------------------------------------ #
    @property
    def version_string(self) -> str:
        return str(self.version)

    def supports_feature(self, feature: str) -> bool:
        """当前版本是否支持某项 MySQL 能力（见 :data:`MYSQL_FEATURES`）。"""
        key = str(feature or "").strip().lower()
        if key not in MYSQL_FEATURES:
            raise InvalidArgumentException(
                f"未知的 MySQL 能力: {feature!r}。可选: "
                + ", ".join(sorted(MYSQL_FEATURES)))
        if self.version < MYSQL_FEATURES[key]:
            return False
        removed = MYSQL_REMOVED.get(key)
        if removed is not None and self.version >= removed:
            return False
        return True

    def require_feature(self, feature: str) -> None:
        """能力不足时抛出带版本信息的 ``UnsupportedOperation``。"""
        from .base import UnsupportedOperation
        key = str(feature or "").strip().lower()
        if self.supports_feature(key):
            return
        minimum = MYSQL_FEATURES[key]
        removed = MYSQL_REMOVED.get(key)
        if removed is not None and self.version >= removed:
            raise UnsupportedOperation(
                f"{key} 已在 MySQL {removed.major}.{removed.minor} 移除"
                f"（当前配置版本 {self.version}）")
        raise UnsupportedOperation(
            f"{key} 需要 MySQL >= {minimum}，当前配置版本 {self.version}；"
            f"请在 Db.config(version=...) 中声明实际版本")

    def features(self) -> Dict[str, Dict[str, Any]]:
        """能力矩阵快照：``{功能: {min, removed_in, supported}}``。"""
        out: Dict[str, Dict[str, Any]] = {}
        for name, minimum in MYSQL_FEATURES.items():
            removed = MYSQL_REMOVED.get(name)
            out[name] = {
                "min": str(minimum),
                "removed_in": str(removed) if removed else None,
                "supported": self.supports_feature(name),
            }
        return out

    def dialect_info(self) -> Dict[str, Any]:
        """方言概况（版本 / 字符集 / 排序规则 / 关键能力）。"""
        return {
            "driver": self.name,
            "version": str(self.version),
            "supported_range": MYSQL_SUPPORTED,
            "charset": self.config.options.get("charset", DEFAULT_CHARSET),
            "collation": self.config.options.get(
                "collation", DEFAULT_COLLATIONS.get(str(self.version.major), "")),
            "paramstyle": self.paramstyle,
            "features": self.features(),
        }

    # ------------------------------------------------------------------ #
    # 连接
    # ------------------------------------------------------------------ #
    def _connect_kwargs(self) -> Dict[str, Any]:
        kwargs = {k: v for k, v in self.config.options.items()
                  if k in MYSQL_CONNECT_KEYS}
        kwargs.setdefault("charset", DEFAULT_CHARSET)
        if self.config.connect_timeout is not None:
            kwargs.setdefault("connect_timeout", self.config.connect_timeout)
        return kwargs

    def connect(self) -> Any:
        if self._conn is not None:
            return self._conn
        try:
            import pymysql
            from pymysql.cursors import DictCursor
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise DriverNotAvailable(
                "MySQL 驱动需要 pymysql：pip install pymysql "
                "（或 pip install tinkpyorm[mysql]）") from exc

        database = self.config.database
        if not database or database == ":memory:":
            database = None      # 未指定库名时不传，避免把 SQLite 默认值传进去
        self._conn = pymysql.connect(
            host=self.config.host or "127.0.0.1",
            port=self.config.port or 3306,
            user=self.config.user or "root",
            password=self.config.password or "",
            database=database,
            cursorclass=DictCursor,
            autocommit=False,   # 事务由 Connection 显式控制
            **self._connect_kwargs())
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def ping(self) -> bool:
        if self._conn is None:
            return False
        try:
            self._conn.ping(reconnect=False)
            return True
        except Exception:
            return False

    def is_connected(self) -> bool:
        return self._conn is not None and bool(getattr(self._conn, "open", False))

    @property
    def raw_connection(self) -> Any:
        return self.connect()

    # ------------------------------------------------------------------ #
    # 执行
    # ------------------------------------------------------------------ #
    def select(self, sql: str, params: Sequence[Any] = ()) -> List[dict]:
        cur = self.connect().cursor()
        try:
            cur.execute(sql, tuple(params) if params else None)
            return [dict(r) for r in cur.fetchall()]
        finally:
            cur.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = self.connect().cursor()
        try:
            cur.execute(sql, tuple(params) if params else None)
            return cur.rowcount
        finally:
            cur.close()

    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        cur = self.connect().cursor()
        try:
            cur.execute(sql, tuple(params) if params else None)
            return cur.lastrowid
        finally:
            cur.close()

    def select_stream(self, sql: str, params: Sequence[Any] = (),
                      chunk_size: int = 1000) -> Iterator[List[dict]]:
        """服务端游标（SSCursor）逐块读取，内存与结果集大小无关。"""
        from pymysql.cursors import SSCursor
        cur = self.connect().cursor(SSCursor)
        try:
            cur.execute(sql, tuple(params) if params else None)
            while True:
                batch = cur.fetchmany(chunk_size)
                if not batch:
                    break
                yield [dict(r) for r in batch]
        finally:
            cur.close()

    # ------------------------------------------------------------------ #
    # 事务（交给 pymysql，避免把 BEGIN 当普通语句执行）
    # ------------------------------------------------------------------ #
    def begin(self) -> None:
        self.connect().begin()

    def commit(self) -> None:
        if self._conn is not None:
            self._conn.commit()

    def rollback(self) -> None:
        if self._conn is not None:
            self._conn.rollback()

    # ------------------------------------------------------------------ #
    # 方言
    # ------------------------------------------------------------------ #
    def placeholder(self, count: int = 1) -> str:
        return ", ".join(["%s"] * max(1, int(count)))

    def _wrap_identifier(self, name: str) -> str:
        return "`" + str(name).replace("`", "``") + "`"

    def limit_sql(self, limit: Any, offset: Optional[int] = None) -> str:
        """``LIMIT n OFFSET m``（MySQL 同时接受 ``LIMIT m, n``，前者更清晰）。"""
        sql = f"LIMIT {int(limit)}"
        if offset:
            sql += f" OFFSET {int(offset)}"
        return sql

    def render_sql(self, sql: str, params: Sequence[Any]) -> str:
        """把 ``%s`` 占位符替换为字面量（仅日志/调试，非执行路径）。"""
        out: List[str] = []
        idx = 0
        i = 0
        while i < len(sql):
            if sql.startswith("%s", i) and idx < len(params):
                out.append(self._render_value(params[idx]))
                idx += 1
                i += 2
                continue
            if sql.startswith("%%", i):
                out.append("%")
                i += 2
                continue
            out.append(sql[i])
            i += 1
        return "".join(out)

    def table_exists(self, table: str) -> bool:
        rows = self.select(
            "SELECT 1 AS ok FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s", (table,))
        return bool(rows)

    def table_fields(self, table: str) -> List[str]:
        rows = self.select(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s "
            "ORDER BY ordinal_position", (table,))
        return [r["name"] for r in rows]

    # ------------------------------------------------------------------ #
    # JSON 方言
    # ------------------------------------------------------------------ #
    #: MySQL 8.0.19 起 ``VALUES(col)`` 被行别名取代（8.0.20 标记弃用）
    def upsert_values_ref(self, column: str) -> str:
        """upsert 时引用"待插入值"的写法，随版本降级/升级。"""
        if self.supports_feature("values_alias"):
            return f"new.{self.quote_identifier(column)}"
        return f"VALUES({self.quote_identifier(column)})"

    def json_extract(self, column_sql: str, path: Any) -> str:
        return f"JSON_EXTRACT({column_sql}, {json_path_literal(path)})"

    def json_exists(self, column_sql: str, path: Any) -> str:
        return (f"(JSON_CONTAINS_PATH({column_sql}, 'one', "
                f"{json_path_literal(path)}) = 1)")

    def json_contains(self, column_sql: str, path: Any,
                      placeholder: str = "%s") -> str:
        """``JSON_CONTAINS(col, CAST(%s AS JSON), '$.tags')``。

        绑定值由 :meth:`json_contains_value` 序列化为 **JSON 文本**——
        MySQL 的候选值必须是合法 JSON，裸字符串 ``vip`` 会报 3141。
        """
        return (f"(JSON_CONTAINS({column_sql}, CAST({placeholder} AS JSON), "
                f"{json_path_literal(path)}) = 1)")

    def json_contains_value(self, value: Any) -> Any:
        if isinstance(value, (dict, list)):
            raise QueryError(
                "JSON 条件不支持 dict/list 直接比较：数组元素匹配请用 "
                "where_json_contains()，对象包含请用 where_raw()")
        return json.dumps(value, ensure_ascii=False)

    def json_length(self, column_sql: str, path: Any) -> str:
        return f"IFNULL(JSON_LENGTH({column_sql}, {json_path_literal(path)}), 0)"

    def json_type(self, column_sql: str, path: Any) -> str:
        """把 MySQL 的类型名归一为 SQLite 的词汇（text / real / true / false）。"""
        expr = f"JSON_EXTRACT({column_sql}, {json_path_literal(path)})"
        kind = f"LOWER(JSON_TYPE({column_sql}, {json_path_literal(path)}))"
        return (f"(CASE {kind} "
                f"WHEN 'string' THEN 'text' "
                f"WHEN 'double' THEN 'real' "
                f"WHEN 'decimal' THEN 'real' "
                f"WHEN 'boolean' THEN IF({expr}, 'true', 'false') "
                f"WHEN 'null' THEN 'null' "
                f"ELSE {kind} END)")

    @staticmethod
    def _json_base(column_sql: str, ifnull: Any) -> str:
        """``JSON_SET(NULL, ...)`` 返回 NULL，空列需 COALESCE 兜底。"""
        if ifnull is None:
            return column_sql
        if isinstance(ifnull, dict) and not ifnull:
            return f"COALESCE({column_sql}, JSON_OBJECT())"
        if isinstance(ifnull, (list, tuple)) and not ifnull:
            return f"COALESCE({column_sql}, JSON_ARRAY())"
        raise InvalidArgumentException(
            "ifnull 仅接受空 dict（视为空对象）或空 list（视为空数组）")

    def json_set(self, column_sql: str, pairs: Sequence[Any],
                 ifnull: Any = None) -> str:
        pairs = list(pairs)
        if not pairs:
            raise InvalidArgumentException("json_set 至少需要一组路径与值")
        base = self._json_base(column_sql, ifnull)
        args = ", ".join(f"{path}, {value_sql}" for path, value_sql in pairs)
        return f"JSON_SET({base}, {args})"

    def json_insert(self, column_sql: str, pairs: Sequence[Any],
                    ifnull: Any = None) -> str:
        pairs = list(pairs)
        if not pairs:
            raise InvalidArgumentException("json_insert 至少需要一组路径与值")
        base = self._json_base(column_sql, ifnull)
        args = ", ".join(f"{path}, {value_sql}" for path, value_sql in pairs)
        return f"JSON_INSERT({base}, {args})"

    def json_remove(self, column_sql: str, paths: Sequence[str],
                    ifnull: Any = None) -> str:
        paths = list(paths)
        if not paths:
            raise InvalidArgumentException("json_remove 至少需要一个路径")
        base = self._json_base(column_sql, ifnull)
        return f"JSON_REMOVE({base}, {', '.join(paths)})"

    def json_patch(self, column_sql: str, value_sql: str,
                   ifnull: Any = None) -> str:
        self.require_feature("json_merge_patch")
        base = self._json_base(column_sql, ifnull)
        return f"JSON_MERGE_PATCH({base}, {value_sql})"

    def json_bind(self, placeholder: str, value: Any) -> str:
        """容器与布尔以 ``CAST(%s AS JSON)`` 绑定，保证 JSON 类型保真。"""
        if isinstance(value, (dict, list, tuple, bool)):
            return f"CAST({placeholder} AS JSON)"
        return placeholder

    def json_decode(self, value: Any) -> Any:
        return super().json_decode(value)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<MySQLDriver {self.config.database} v{self.version}>"
