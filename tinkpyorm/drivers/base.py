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
import json
import re
from typing import Any, Dict, Iterator, List, Optional, Sequence

from ..config import Config
from ..exceptions import InvalidArgumentException, OrmError

# 合法裸标识符（用于区分字段名 vs 表达式）：仅字母数字下划线，可含点
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# ---------------------------------------------------------------------- #
# JSON 路径工具（方言无关，所有支持 JSON 的驱动共用）
# ---------------------------------------------------------------------- #
#: JSON 根路径
JSON_ROOT = "$"

#: 合法 JSON 路径：$ / $.key / $."key" / $[0] / $[#]，可任意组合
_JSON_PATH_RE = re.compile(
    r"^\$(?:"
    r'\.(?:[A-Za-z_][A-Za-z0-9_]*|"[^"]*")'
    r"|\[\d+\]"
    r"|\[#\]"
    r")*$"
)


def normalize_json_path(path: Any) -> str:
    """把各种 JSON 路径写法统一为 ``$`` 开头的标准路径。

    接受并归一化以下写法::

        normalize_json_path(None)        -> '$'
        normalize_json_path('')          -> '$'
        normalize_json_path('$')         -> '$'
        normalize_json_path('user.name') -> '$.user.name'
        normalize_json_path('.user')     -> '$.user'
        normalize_json_path('[0].id')    -> '$[0].id'

    路径不合法时抛 :class:`InvalidArgumentException`，避免把任意字符串
    拼进 SQL（本函数是 JSON 路径进入 SQL 前的唯一安全闸口）。
    """
    if path is None:
        return JSON_ROOT
    s = str(path).strip()
    if not s or s == JSON_ROOT:
        return JSON_ROOT
    if not s.startswith("$"):
        s = "$" + (s if s.startswith("[") else "." + s.lstrip("."))
    if not _JSON_PATH_RE.match(s):
        raise InvalidArgumentException(
            f"非法 JSON 路径: {path!r}。合法形式示例: '$'、'user.name'、"
            f"'$.items[0]'、'$.\"带空格的键\"'")
    return s


def json_path_literal(path: Any) -> str:
    """把 JSON 路径渲染为 SQL 字符串字面量（含引号转义）。

    路径经 :func:`normalize_json_path` 白名单校验后才会内联；
    SQLite / MySQL 的 ``json_extract`` 家族要求路径是字面量或绑定值，
    此处采用"校验 + 内联"以保证 SQL 结构可读且无注入风险。
    """
    return "'" + normalize_json_path(path).replace("'", "''") + "'"


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

    def select_stream(self, sql: str, params: Sequence[Any] = (),
                      chunk_size: int = 1000) -> Iterator[List[dict]]:
        """流式执行 SELECT，按 ``chunk_size`` 分块产出 dict 列表。

        默认实现为"整体查询后分块"，仅保证接口可用；具备游标能力的驱动
        应覆写本方法（参照 ``SQLiteDriver.select_stream``）以实现真流式，
        避免大结果集整体载入内存。
        """
        rows = self.select(sql, params)
        for start in range(0, len(rows), chunk_size):
            yield rows[start:start + chunk_size]

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

    # ------------------------------------------------------------------ #
    # JSON 能力（可选）
    # ------------------------------------------------------------------ #
    #: 是否支持 JSON 字段的路径查询。
    #:
    #: - SQLite / MySQL / PostgreSQL：True（实现下方方法即可）
    #: - MongoDB / Redis 等 NoSQL：查询走原生客户端，本组方法不适用
    #:   （``NoSQLDriver`` 已覆写为"原样返回 Python 对象"语义）
    supports_json: bool = False

    # 下述方法返回**方言级 SQL 表达式片段**，参数一律由 Builder 通过
    # ``placeholder`` 提供的占位符绑定，驱动不得自行内联用户数据。
    #
    # 扩展新数据库时只需覆写本组方法（参照 SQLiteDriver）：
    #   MySQL   : JSON_EXTRACT(col, path) / JSON_CONTAINS / JSON_LENGTH
    #             JSON_SET / JSON_INSERT / JSON_REMOVE / JSON_MERGE_PATCH
    #   Postgres: col #> path / col @> value::jsonb / jsonb_array_length
    #             jsonb_set / col - path / col || patch
    #   MongoDB : 由 NoSQLDriver 承接，走原生 dict 查询，不产生 SQL
    #             （路径写入映射为 $set / $unset 更新算子）
    #
    # 查询侧：json_extract / json_exists / json_contains / json_length /
    #         json_type
    # 写入侧：json_set / json_insert / json_remove / json_patch
    # 编解码：json_decode / json_encode / json_bind
    def json_extract(self, column_sql: str, path: Any) -> str:
        """返回"提取 JSON 路径值"的 SQL 表达式。"""
        raise UnsupportedOperation(
            f"{self.name} 驱动未实现 JSON 路径查询（supports_json=False）")

    def json_exists(self, column_sql: str, path: Any) -> str:
        """返回"JSON 路径是否存在"的布尔表达式。

        语义为**路径存在**，与"路径值是否为 JSON null"区分：
        路径存在但值为 ``null`` 时本表达式仍为真。
        """
        raise UnsupportedOperation(
            f"{self.name} 驱动未实现 JSON 路径存在判断")

    def json_contains(self, column_sql: str, path: Any,
                      placeholder: str = "?") -> str:
        """返回"JSON 数组是否包含某个标量元素"的布尔表达式。"""
        raise UnsupportedOperation(
            f"{self.name} 驱动未实现 JSON 数组包含判断")

    def json_length(self, column_sql: str, path: Any) -> str:
        """返回 JSON 数组 / 对象的元素个数表达式。"""
        raise UnsupportedOperation(
            f"{self.name} 驱动未实现 JSON 长度计算")

    def json_type(self, column_sql: str, path: Any) -> str:
        """返回 JSON 路径值的类型名表达式。

        SQLite / MySQL 返回 ``'null'`` / ``'true'`` / ``'false'`` /
        ``'integer'`` / ``'real'`` / ``'text'`` / ``'array'`` / ``'object'``。
        """
        raise UnsupportedOperation(
            f"{self.name} 驱动未实现 JSON 类型判断")

    # ---- JSON 路径写入（局部更新） ---- #
    # 与上方"路径查询"对称：查询用 json_extract 取值，
    # 写入用 json_set 族在 SQL 内改写嵌套字段——省掉"先读出整列"的一次
    # 往返与 Python 侧解析，并避免 read-modify-write 在并发交错下的丢失
    # 更新（读改写方式下，后写者会整体覆盖先写者的改动）。
    # 注意：存储层仍会重写整列 JSON 文本，写入放大与读改写相同。
    #
    # ``ifnull`` 参数用于处理"列为 SQL NULL"的边界：SQLite / MySQL 的
    # json_set(NULL, ...) 会返回 NULL（静默无效）。传入空文档后驱动
    # 以 COALESCE 兜底，使首批写入在空列上也能生效。
    def json_set(self, column_sql: str, pairs: Sequence[Any],
                 ifnull: Any = None) -> str:
        """返回"按路径写入值（存在则覆盖、不存在则新增）"的表达式。

        ``pairs`` 为 ``[(路径字面量, 值占位符), ...]``：路径已由
        :func:`json_path_literal` 校验并转义，值片段由 :meth:`json_bind`
        生成，二者均由 Builder 提供，驱动不得内联用户数据。

        多个 ``(路径, 值)`` 必须编译进**同一次调用**，因为 SQL 中同一列
        出现多个 SET 子句时只有最后一个生效（其余静默丢失）。
        """
        raise UnsupportedOperation(
            f"{self.name} 驱动未实现 JSON 路径写入")

    def json_insert(self, column_sql: str, pairs: Sequence[Any],
                    ifnull: Any = None) -> str:
        """返回"按路径写入值（**仅当路径不存在时**）"的表达式。

        与 :meth:`json_set` 的区别是**不覆盖已有值**，适合初始化默认字段。
        """
        raise UnsupportedOperation(
            f"{self.name} 驱动未实现 JSON 路径插入")

    def json_remove(self, column_sql: str, paths: Sequence[str],
                    ifnull: Any = None) -> str:
        """返回"删除指定路径（可多个）"的表达式。

        路径不存在时静默忽略，不报错。
        """
        raise UnsupportedOperation(
            f"{self.name} 驱动未实现 JSON 路径删除")

    def json_patch(self, column_sql: str, value_sql: str,
                   ifnull: Any = None) -> str:
        """返回"按 RFC 7396 JSON Merge Patch 合并文档"的表达式。

        ``value_sql`` 为补丁文档的绑定片段（见 :meth:`json_bind`）。

        **语义提示**：RFC 7396 中补丁里的 ``null`` 表示**删除该键**，
        与 :meth:`json_set` 传入 ``null`` 表示"置为 JSON null"相反。
        """
        raise UnsupportedOperation(
            f"{self.name} 驱动未实现 JSON 文档合并")

    def json_bind(self, placeholder: str, value: Any) -> str:
        """把占位符转换为"以 JSON 值绑定"的 SQL 片段。

        默认原样返回（适用于把 JSON 以文本绑定的驱动）。需要类型化绑定的
        驱动应覆写，例如 SQLite 用 ``json(?)`` 把文本参数解析为 JSON 值
        （否则嵌套对象会被存成**转义字符串**、``True`` 会退化成整数 1），
        MySQL 用 ``CAST(? AS JSON)``。

        ``value`` 为原始 Python 值，驱动据此判断是否需要包装；容器与布尔
        由 Builder 连同 **JSON 文本**一并绑定（见 ``Builder._json_bind_value``），
        因此本方法的返回片段需与之一致。
        """
        return placeholder

    # ---- 值编解码（Python 层，与方言无关，通常无需覆写） ---- #
    def json_decode(self, value: Any) -> Any:
        """把数据库返回的 JSON 值解码为 Python 对象（dict / list / 标量）。

        默认实现：``str`` / ``bytes`` 尝试 ``json.loads``，失败则原样返回；
        已经是 ``dict`` / ``list`` 的值直接返回（幂等）。

        MongoDB / Redis 等原生返回 Python 对象的驱动应覆写为恒等函数，
        避免把普通字符串误解析为 JSON。
        """
        if isinstance(value, (bytes, bytearray)):
            try:
                value = value.decode("utf-8")
            except UnicodeDecodeError:
                return value
        if isinstance(value, (dict, list)):
            return value
        if isinstance(value, str):
            s = value.strip()
            if len(s) < 2 or s[0] not in "[{\"" or s[-1] not in "]}\"":
                # 快速排除：JSON 文档/字符串必以 { [ " 开头并以 } ] " 结尾
                return value
            try:
                return json.loads(s)
            except (ValueError, TypeError):
                return value
        return value

    def json_encode(self, value: Any) -> Any:
        """把 Python 对象编码为可绑定参数。

        ``dict`` / ``list`` / ``tuple`` -> JSON 文本（``tuple`` 视为 JSON
        数组：底层驱动无法绑定元组，不转换只会得到晦涩的绑定错误）；
        其余值原样返回（交由底层驱动绑定）。

        ``set`` / ``frozenset`` 不在此处转换：其元素无序，转成 JSON 数组
        后顺序不确定，结果不可复现。
        """
        if isinstance(value, (dict, list, tuple)):
            return json.dumps(value, ensure_ascii=False)
        return value

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

    JSON 语义：MongoDB / Redis 返回的对象本身已是 Python ``dict`` / ``list``，
    因此 :meth:`json_decode` / :meth:`json_encode` 为恒等函数——
    上层"结果自动格式化为 dict"的代码路径无需任何分支即可复用。
    """

    supports_transactions = False
    supports_json = True

    def json_decode(self, value: Any) -> Any:
        """NoSQL 返回值已是 Python 对象，原样透传（不做字符串解析）。"""
        return value

    def json_encode(self, value: Any) -> Any:
        """NoSQL 客户端直接接受 Python 对象，原样透传。"""
        return value

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
