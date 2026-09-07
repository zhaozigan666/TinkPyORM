"""驱动抽象层：把「连接建立 / SQL 执行 / 事务原语 / SQL 方言」与 ORM 解耦。

新增一种数据库只需实现本模块中的 :class:`Driver` 接口并注册，
上层（Connection / Builder / Query / Model）无需改动。

驱动分两类：

* :class:`SQLDriver` —— 关系型数据库，完整支持查询构造器与事务；
* :class:`NoSQLDriver` —— 非关系型（MongoDB / Redis 等），只提供连接管理，
  查询构造器与事务不可用，通过 ``raw_connection`` 使用原生客户端。
"""
from __future__ import annotations

import abc
import re
from typing import Any, Dict, List, Optional, Sequence

from ..config import Config
from ..exceptions import OrmError

# 合法裸标识符（用于区分字段名 vs 表达式）：仅字母数字下划线，可含点
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class UnsupportedOperation(OrmError, NotImplementedError):
    """当前驱动不支持该操作（如 Redis 不支持 SQL 查询构造器）。"""


class Driver(abc.ABC):
    """数据库驱动接口。

    必选实现：``connect`` / ``close`` / ``select`` / ``execute`` / ``insert``。
    其余方法提供合理默认，按需覆写。
    """

    #: 驱动名（与 Config.type 对应）
    name: str = ""
    #: 参数占位风格：qmark(?) / format(%s) / pyformat(%(name)s) / numeric(:1)
    paramstyle: str = "qmark"
    #: 是否支持事务
    supports_transactions: bool = True

    def __init__(self, config: Config) -> None:
        self.config = config

    # ------------------------------------------------------------------ #
    # 连接生命周期
    # ------------------------------------------------------------------ #
    @abc.abstractmethod
    def connect(self) -> Any:
        """建立并返回底层连接对象。"""

    @abc.abstractmethod
    def close(self) -> None:
        """关闭底层连接。"""

    def ping(self) -> bool:
        """连接是否可用。"""
        raise UnsupportedOperation(f"{self.name} 驱动未实现 ping()")

    def is_connected(self) -> bool:
        """底层连接是否已建立（未连接/已关闭均为 False）。"""
        return False

    @property
    def raw_connection(self) -> Any:
        """底层原生连接（逃生舱，用于绕过 ORM 直接使用驱动）。"""
        raise UnsupportedOperation(f"{self.name} 驱动未暴露原生连接")

    # ------------------------------------------------------------------ #
    # SQL 执行
    # ------------------------------------------------------------------ #
    @abc.abstractmethod
    def select(self, sql: str, params: Sequence[Any] = ()) -> List[dict]:
        """执行 SELECT，返回 dict 列表。"""

    @abc.abstractmethod
    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行写语句，返回影响行数（不自动提交）。"""

    @abc.abstractmethod
    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        """执行 INSERT，返回自增主键（不自动提交）。"""

    # ------------------------------------------------------------------ #
    # 事务原语
    # ------------------------------------------------------------------ #
    def begin(self) -> None:
        self.execute_raw("BEGIN")

    def commit(self) -> None:
        self.execute_raw("COMMIT")

    def rollback(self) -> None:
        self.execute_raw("ROLLBACK")

    def savepoint(self, name: str) -> None:
        self.execute_raw(f"SAVEPOINT {name}")

    def release(self, name: str) -> None:
        self.execute_raw(f"RELEASE SAVEPOINT {name}")

    def rollback_to(self, name: str) -> None:
        self.execute_raw(f"ROLLBACK TO SAVEPOINT {name}")

    def execute_raw(self, sql: str) -> None:
        """执行不返回结果的事务控制语句（默认走 execute）。"""
        self.execute(sql)

    # ------------------------------------------------------------------ #
    # SQL 方言
    # ------------------------------------------------------------------ #
    def placeholder(self, count: int = 1) -> str:
        """生成 ``count`` 个占位符（逗号分隔）。"""
        return ", ".join("?" * count)

    def quote_identifier(self, name: str) -> str:
        """引用标识符（表名 / 字段名）。

        智能识别：

        - ``*`` 原样返回（通配符）
        - 简单标识符 ``id`` / ``user.id`` 加引号
        - 复杂表达式（``COUNT(*)``、``name AS n``）原样返回，
          调用方应确保来源可信（开发者代码而非用户输入）
        """
        if not isinstance(name, str):
            return str(name)
        s = name.strip()
        if s == "*":
            return "*"
        if _IDENT_RE.match(s):
            return self._wrap_identifier(s)
        if "." in s and all(_IDENT_RE.match(p) for p in s.split(".") if p):
            return ".".join(self._wrap_identifier(p) for p in s.split("."))
        # 表达式原样返回（防注入由参数绑定层保证）
        return s

    def _wrap_identifier(self, name: str) -> str:
        """各驱动覆写此方法以提供方言级引号。"""
        return f"`{name}`"

    def limit_sql(self, limit: Any, offset: Optional[int] = None) -> str:
        """生成分页子句。"""
        sql = f"LIMIT {int(limit)}"
        if offset:
            sql += f" OFFSET {int(offset)}"
        return sql

    def table_exists(self, table: str) -> bool:
        raise UnsupportedOperation(f"{self.name} 驱动未实现 table_exists()")

    def table_fields(self, table: str) -> List[str]:
        raise UnsupportedOperation(f"{self.name} 驱动未实现 table_fields()")

    def render_sql(self, sql: str, params: Sequence[Any]) -> str:
        """把参数内联进 SQL，仅供日志/调试展示（非执行路径）。"""
        out: List[str] = []
        idx = 0
        for ch in sql:
            if ch == "?" and idx < len(params):
                out.append(self._render_value(params[idx]))
                idx += 1
            else:
                out.append(ch)
        return "".join(out)

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _render_value(value: Any) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, (int, float)):
            return str(value)
        return "'" + str(value).replace("'", "''") + "'"

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{self.__class__.__name__} {self.config.type}:{self.config.database}>"


class SQLDriver(Driver):
    """关系型数据库驱动的公共基类（SQLite 风格默认方言）。"""

    supports_transactions = True

    def ping(self) -> bool:
        try:
            self.select("SELECT 1")
            return True
        except Exception:
            return False


class NoSQLDriver(Driver):
    """非关系型数据库驱动基类。

    仅提供连接管理；SQL 相关能力一律抛 :class:`UnsupportedOperation`，
    避免产生"能构造 SQL 但语义错误"的假支持。使用者应通过
    ``raw_connection`` 访问原生客户端（如 ``redis.Redis``）。
    """

    supports_transactions = False

    def select(self, sql: str, params: Sequence[Any] = ()) -> List[dict]:
        raise UnsupportedOperation(
            f"{self.name} 不是关系型数据库，不支持 SQL 查询构造器；"
            f"请使用 connection().driver.raw_connection 访问原生客户端")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        raise UnsupportedOperation(
            f"{self.name} 不是关系型数据库，不支持 SQL 执行")

    def insert(self, sql: str, params: Sequence[Any] = ()) -> int:
        raise UnsupportedOperation(
            f"{self.name} 不是关系型数据库，不支持 SQL 插入")

    def begin(self) -> None:
        raise UnsupportedOperation(f"{self.name} 不支持 SQL 事务")

    def commit(self) -> None:
        raise UnsupportedOperation(f"{self.name} 不支持 SQL 事务")

    def rollback(self) -> None:
        raise UnsupportedOperation(f"{self.name} 不支持 SQL 事务")
