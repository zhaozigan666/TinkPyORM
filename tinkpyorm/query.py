"""查询构造器（对应 think-orm 的 BaseQuery/Query）。

核心设计复刻 think-orm：所有链式方法修改内部 ``options`` 字典，
终端方法将 options 交给 Builder 编译为 SQL 并执行。所有查询值
一律参数绑定，杜绝 SQL 注入。

JSON 能力（v0.5.0）：``where_json`` 家族提供 JSON 路径条件查询，
``json()`` 控制结果的自动格式化（解析为 Python dict / list），
底层 SQL 形态由驱动方言提供，可扩展至 MySQL / PostgreSQL 等。
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from . import cache as _cache
from .builder import (
    Builder, JsonUpdate, JsonWhere, JSON_OPS, JSON_UPDATE_MODES, RawWhere,
)
from .connection import Connection
from .drivers import UnsupportedOperation, normalize_json_path
from .exceptions import DataNotFound, InvalidArgumentException, QueryError
from .utils import Raw, UNSET, raw, to_snake

# SQLite 单语句绑定变量上限（SQLITE_MAX_VARIABLE_NUMBER）
MAX_SQL_VARIABLES = 32766

# JSON API 的列名/别名白名单：裸标识符，可带一层限定名（表.列）
# 校验先于引用，避免非法名被 quote_identifier 当作"表达式原样返回"而注入
_JSON_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?$")
_JSON_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 参数未显式传入的哨兵（用于区分"省略运算符/值"与"传了 None"）
#: 定义在 utils 中以便 Model 层复用同一对象（身份比较必须是同一实例）
_UNSET = UNSET


class Query:
    """链式查询构造器。"""

    def __init__(
        self,
        conn: Optional[Connection] = None,
        table: Optional[str] = None,
        prefix: str = "",
        model: Optional[type] = None,
    ):
        self.conn = conn
        self.builder = Builder(conn) if conn else None
        self.prefix = prefix
        self.model = model  # 绑定的模型类（结果自动转换为模型实例）
        self.options: dict = {
            "table": [],        # [{'name':..., 'alias':...}, ...]
            "where": [],        # [(logic, cond), ...]
            "field": None,
            "json_field": [],   # JSON 路径字段 [(列, 路径, 别名), ...]，由 Builder 合并
            "without_field": None,
            "order": None,
            "limit": None,
            "join": [],
            "group": None,
            "having": [],
            "union": [],
            "distinct": False,
            "lock": None,
            "comment": None,
            "alias": None,
            "data": None,       # insert/update 数据
            "json_update": [],  # JSON 路径写入规格 [JsonUpdate, ...]
            "json": [],         # JSON 解码字段：[] 关闭 / [field,...] 指定 / True 自动嗅探
            "attr": {},         # withAttr 获取器 {field: callable}
            "filter": [],       # filter 回调列表
            "fetch_sql": False,
            "cache": None,      # (key, expire)
            "allow_empty": False,
            "fail_exception": False,
            "with": [],         # 预载入关联 ['posts', {'posts': closure}]
            "soft_delete": None,
        }
        self._last_sql = ""
        self._last_params: list = []
        self._soft_applied = False
        if table:
            self.table(table)

    # ------------------------------------------------------------------ #
    # 表与字段
    # ------------------------------------------------------------------ #
    def table(self, table: str) -> "Query":
        """指定表名（含前缀）。支持别名 ``'user u'``。"""
        self.options["table"] = [self._parse_table(table)]
        return self

    def name(self, table: str) -> "Query":
        """指定表名（自动补前缀）。"""
        return self.table(self._resolved_prefix() + table)

    def alias(self, alias: str) -> "Query":
        """设置当前表别名。"""
        if self.options["table"]:
            self.options["table"][0]["alias"] = alias
        else:
            self.options["alias"] = alias
        return self

    @staticmethod
    def _parse_table(table: str) -> dict:
        parts = table.strip().split()
        if len(parts) == 1:
            return {"name": parts[0], "alias": None}
        return {"name": parts[0], "alias": " ".join(parts[1:]).replace("AS ", "").replace("as ", "")}

    def field(self, *fields: Any) -> "Query":
        """指定查询字段: field('id,name') / field('id','name') / field({'uid': 'id'})。"""
        if not fields:
            return self
        if len(fields) == 1:
            self.options["field"] = fields[0]
        else:
            self.options["field"] = list(fields)
        return self

    def field_raw(self, expr: str) -> "Query":
        """原生字段表达式: field_raw('COUNT(*) as cnt')。"""
        self.options["field"] = Raw(expr)
        return self

    def without_field(self, *fields: str) -> "Query":
        """排除字段（自动基于表结构生成保留字段）。"""
        if len(fields) == 1 and not isinstance(fields[0], str):
            self.options["without_field"] = fields[0]
        else:
            self.options["without_field"] = list(fields)
        return self

    def distinct(self, flag: bool = True) -> "Query":
        self.options["distinct"] = flag
        return self

    def comment(self, comment: str) -> "Query":
        self.options["comment"] = comment
        return self

    def lock(self, flag: Union[bool, str] = True) -> "Query":
        self.options["lock"] = flag
        return self

    # ------------------------------------------------------------------ #
    # WHERE 条件
    # ------------------------------------------------------------------ #
    def where(self, *args: Any, **kwargs: Any) -> "Query":
        """条件过滤（AND 连接）。支持 think-orm 的多种形式::

            where('id', '>', 10)            # 字段 + 运算符 + 值
            where('id', 10)                 # 等于简写
            where('id', [1, 2, 3])          # IN
            where('id', None)               # IS NULL
            where(id=10, status=1)          # 关键字形式
            where({'id': 10, 'status': 1})  # 数组形式
            where(lambda q: q.where(...).where_or(...))  # 闭包分组
        """
        return self._where("AND", *args, **kwargs)

    def where_or(self, *args: Any, **kwargs: Any) -> "Query":
        """OR 连接条件。"""
        return self._where("OR", *args, **kwargs)

    def where_xor(self, *args: Any, **kwargs: Any) -> "Query":
        """XOR 连接条件。"""
        return self._where("XOR", *args, **kwargs)

    def _where(self, logic: str, *args: Any, **kwargs: Any) -> "Query":
        if args:
            self.options["where"].append((logic, self._normalize_where_arg(*args)))
        if kwargs:
            self.options["where"].append((logic, dict(kwargs)))
        return self

    @staticmethod
    def _normalize_where_arg(*args: Any) -> Any:
        if len(args) == 1:
            return args[0]  # dict / 闭包 / Raw / 条件字符串
        if len(args) == 2:
            return (args[0], args[1])  # (field, value) 等于简写
        if len(args) == 3:
            return (args[0], args[1], args[2])  # (field, op, value)
        raise QueryError("where 参数数量不合法")

    def where_null(self, field: str) -> "Query":
        return self._where("AND", (field, "is", None))

    def where_not_null(self, field: str) -> "Query":
        return self._where("AND", (field, "is not", None))

    def where_in(self, field: str, values: Sequence[Any]) -> "Query":
        return self._where("AND", (field, "in", values))

    def where_not_in(self, field: str, values: Sequence[Any]) -> "Query":
        return self._where("AND", (field, "not in", values))

    def where_like(self, field: str, pattern: str) -> "Query":
        return self._where("AND", (field, "like", pattern))

    def where_not_like(self, field: str, pattern: str) -> "Query":
        return self._where("AND", (field, "not like", pattern))

    def where_between(self, field: str, values: Sequence[Any]) -> "Query":
        return self._where("AND", (field, "between", values))

    def where_not_between(self, field: str, values: Sequence[Any]) -> "Query":
        return self._where("AND", (field, "not between", values))

    def where_column(self, field1: str, op: str, field2: str) -> "Query":
        """字段对字段比较: where_column('a.id', '=', 'b.user_id')。"""
        qi = self._resolve_conn().driver.quote_identifier
        return self._where("AND", Raw(f"{qi(field1)} {op.upper()} {qi(field2)}"))

    def where_raw(self, sql: str, params: Sequence[Any] = ()) -> "Query":
        """原生条件 + 参数绑定: where_raw('status = ? AND level > ?', [1, 3])。"""
        self.options["where"].append(("AND", RawWhere(sql, params)))
        return self

    def where_exists(self, callback: Callable[["Query"], None]) -> "Query":
        return self._where("AND", ("__exists__", "exists", callback))

    def where_not_exists(self, callback: Callable[["Query"], None]) -> "Query":
        return self._where("AND", ("__exists__", "not exists", callback))

    # ------------------------------------------------------------------ #
    # 排序 / 分页 / 联表 / 分组
    # ------------------------------------------------------------------ #
    def order(self, field: Any, direction: Optional[str] = None) -> "Query":
        """排序: order('id desc') / order('id', 'desc') / order({'id': 'desc'})。"""
        if direction is not None:
            field = {field: direction}
        if self.options["order"] is None:
            self.options["order"] = field
        else:
            # 多次调用合并（think-orm 追加语义）
            existing = self.options["order"]
            if isinstance(existing, str):
                merged = existing + ", " + (field if isinstance(field, str) else str(field))
            elif isinstance(existing, list):
                merged = existing + ([field] if isinstance(field, str) else [field])
            else:
                merged = {**existing, **(field if isinstance(field, dict) else {})}
            self.options["order"] = merged
        return self

    def order_raw(self, expr: str) -> "Query":
        self.options["order"] = Raw(expr)
        return self

    def limit(self, length: int, offset: Optional[int] = None) -> "Query":
        """分页限制: limit(10) 取前10条；limit(10, 20) 从偏移20取10条。"""
        if offset is None:
            self.options["limit"] = length
        else:
            self.options["limit"] = (length, offset)
        return self

    def page(self, page: int, list_rows: int = 20) -> "Query":
        """页码分页: page(2, 10) 第2页每页10条。"""
        offset = (max(1, page) - 1) * list_rows
        self.options["limit"] = (list_rows, offset)
        return self

    def join(self, table: str, condition: Any, type: str = "INNER") -> "Query":
        """联表: join('profile p', lambda q: q.where_column('p.user_id','=','user.id'))。"""
        self.options["join"].append({"table": table.split()[0], "alias": self._join_alias(table), "on": condition, "type": type})
        return self

    def left_join(self, table: str, condition: Any) -> "Query":
        return self.join(table, condition, "LEFT")

    def right_join(self, table: str, condition: Any) -> "Query":
        return self.join(table, condition, "RIGHT")

    def inner_join(self, table: str, condition: Any) -> "Query":
        return self.join(table, condition, "INNER")

    @staticmethod
    def _join_alias(table: str) -> Optional[str]:
        parts = table.strip().split()
        if len(parts) <= 1:
            return None
        alias = " ".join(parts[1:])
        return alias.replace("AS ", "").replace("as ", "").strip() or None

    def group(self, *fields: Any) -> "Query":
        if len(fields) == 1:
            self.options["group"] = fields[0]
        else:
            self.options["group"] = list(fields)
        return self

    def having(self, *args: Any, **kwargs: Any) -> "Query":
        return self._having("AND", *args, **kwargs)

    def having_or(self, *args: Any, **kwargs: Any) -> "Query":
        return self._having("OR", *args, **kwargs)

    def _having(self, logic: str, *args: Any, **kwargs: Any) -> "Query":
        if args:
            self.options["having"].append((logic, self._normalize_where_arg(*args)))
        if kwargs:
            self.options["having"].append((logic, dict(kwargs)))
        return self

    def union(self, target: Any, all: bool = False) -> "Query":
        """UNION：传入闭包（子查询）或原生 SQL。"""
        self.options["union"].append({"query": target, "all": all})
        return self

    def union_all(self, target: Any) -> "Query":
        """UNION ALL（不去重）：传入闭包（子查询）或原生 SQL。"""
        self.options["union"].append({"query": target, "all": True})
        return self

    # ------------------------------------------------------------------ #
    # 查询选项（结果处理）
    # ------------------------------------------------------------------ #
    def json(self, fields: Union[None, bool, str, Sequence[str]] = None) -> "Query":
        """启用 JSON 字段自动解码（查询结果格式化为 Python 对象）。

        三种模式::

            q.json()                     # 自动嗅探：字形如 JSON 的字符串字段一律解码
            q.json(['extra', 'meta'])    # 指定字段（零嗅探开销，推荐生产使用）
            q.json(False)                # 关闭（显式声明，便于链式切换）

        解码由驱动完成（``driver.json_decode``）：

        - SQLite / MySQL：JSON 文本 -> dict / list / 标量
        - MongoDB / Redis：原生返回 Python 对象，恒等透传，代码路径完全一致

        解码是**幂等**的，且对 ``None`` 直接跳过；已解析的 dict / list 不会
        被二次处理。缓存命中时同样会重新应用本设置，行为与未命中一致。
        """
        if fields is None or fields is True:
            self.options["json"] = True
        elif fields is False:
            self.options["json"] = []
        elif isinstance(fields, str):
            self.options["json"] = [fields]
        else:
            self.options["json"] = list(fields)
        return self

    # ------------------------------------------------------------------ #
    # JSON 路径条件查询（v0.5.0）
    # ------------------------------------------------------------------ #
    def where_json(self, field: str, path: Any = None,
                   op: Any = "=", value: Any = _UNSET) -> "Query":
        """JSON 路径条件（AND 连接）。

        路径写法：``'$.user.name'`` / ``'user.name'`` / ``'[0].id'``，
        统一归一化为 ``$`` 开头。支持两种调用签名::

            # 4 参：字段 + 路径 + 运算符 + 值
            Db.table('user').where_json('extra', '$.age', '>', 18).select()
            Db.table('user').where_json('extra', '$.tags', 'contains', 'vip')
            Db.table('user').where_json('extra', '$.name', 'like', '%张%')
            Db.table('user').where_json('extra', '$.level', 'in', [2, 3])

            # 3 参：省略运算符时视为"等于"（字符串值亦可）
            Db.table('user').where_json('extra', '$.name', '张三')      # = 张三
            Db.table('user').where_json('extra', '$.age', 18)          # = 18
            Db.table('user').where_json('extra', '$.flag', True)       # = true
            Db.table('user').where_json('extra', '$.deleted', 'exists')
            Db.table('user').where_json('extra', '$.del_time', 'is null')

        语义约定：

        - ``value is None`` 且运算符为 ``=`` 时自动转 ``is null``
          （``!=`` 转 ``is not null``），贴合直觉
        - ``is null`` / ``is not null``：路径**值**为 null 或路径不存在
        - ``exists`` / ``not exists``：路径**是否存在**（值为 null 也算存在）
        - ``contains`` / ``not contains``：JSON 数组是否含某标量元素
        - ``True`` / ``False`` 自动映射为 JSON 的 ``true`` / ``false``（存为 1/0）
        - ``dict`` / ``list`` 不支持直接比较（文本比较受键序影响，结果不可靠），
          会显式报错并引导到 ``where_json_contains`` / ``where_raw``

        若第 3 个位置参数恰好等于某运算符名（如字符串 ``'in'``），会被优先
        解释为运算符；需要比较该字符串值时请写全 4 个参数。
        """
        return self._json_where("AND", field, path, op, value)

    def where_json_or(self, field: str, path: Any = None,
                      op: Any = "=", value: Any = _UNSET) -> "Query":
        """JSON 路径条件（OR 连接），签名与 :meth:`where_json` 相同。"""
        return self._json_where("OR", field, path, op, value)

    def where_json_null(self, field: str, path: Any = None) -> "Query":
        """JSON 路径值为 null 或路径不存在。"""
        return self._json_where("AND", field, path, "is null", None)

    def where_json_not_null(self, field: str, path: Any = None) -> "Query":
        """JSON 路径值非 null。"""
        return self._json_where("AND", field, path, "is not null", None)

    def where_json_exists(self, field: str, path: Any = None) -> "Query":
        """JSON 路径存在（值为 ``null`` 亦算存在）。"""
        return self._json_where("AND", field, path, "exists", None)

    def where_json_not_exists(self, field: str, path: Any = None) -> "Query":
        """JSON 路径不存在。"""
        return self._json_where("AND", field, path, "not exists", None)

    def where_json_contains(self, field: str, path: Any, value: Any) -> "Query":
        """JSON 数组包含某标量元素::

            Db.table('user').where_json_contains('extra', '$.tags', 'vip').select()
        """
        return self._json_where("AND", field, path, "contains", value)

    def where_json_not_contains(self, field: str, path: Any, value: Any) -> "Query":
        """JSON 数组不包含某标量元素。"""
        return self._json_where("AND", field, path, "not contains", value)

    def where_json_length(self, field: str, path: Any,
                          op: Any = _UNSET, value: Any = _UNSET) -> "Query":
        """按 JSON 数组 / 对象的元素个数过滤::

            q.where_json_length('extra', '$.tags', '>=', 3)
            q.where_json_length('extra', '$.tags', 5)      # 省略运算符 = 等于 5

        路径不存在时长度为 0（与空数组不可区分）。
        """
        self._require_json()
        field = self._json_ident(field, "where_json_length 的列名")
        if op is _UNSET:
            raise QueryError(
                "where_json_length 需要运算符或数值，"
                "例如 where_json_length('extra', '$.tags', '>=', 3)")
        if value is _UNSET:
            # 3 参形式：第 3 个位置参数是数值，默认等值比较
            value, op = op, "="
        elif not (isinstance(op, str) and op.strip().lower() in JSON_OPS):
            raise QueryError(
                f"不支持的 JSON 条件运算符: {op!r}。可选: "
                + ", ".join(sorted(JSON_OPS)))
        self.options["where"].append(
            ("AND", JsonWhere(field, path, op, value, func="length")))
        return self

    def where_json_type(self, field: str, path: Any, type_name: str) -> "Query":
        """按 JSON 路径值的类型过滤::

            q.where_json_type('extra', '$.tags', 'array')
            q.where_json_type('extra', '$.user', 'object')
            q.where_json_type('extra', '$.deleted', 'null')

        类型名：``null`` / ``true`` / ``false`` / ``integer`` / ``real`` /
        ``text`` / ``array`` / ``object``（SQLite 与 MySQL 命名一致）。
        """
        if not isinstance(type_name, str) or not type_name.strip():
            raise InvalidArgumentException("where_json_type 需要类型名字符串")
        self._require_json()
        field = self._json_ident(field, "where_json_type 的列名")
        self.options["where"].append(
            ("AND", JsonWhere(field, path, "=", type_name.strip().lower(),
                              func="type")))
        return self

    def field_json(self, field: str, path: Any = None,
                   alias: Optional[str] = None) -> "Query":
        """选取 JSON 路径值作为查询字段（结果自动解码为 Python 对象）::

            Db.table('user').field_json('extra', '$.city').select()
            # SELECT json_extract(`extra`, '$.city') AS `city` FROM `user`
            # -> Collection([{'city': '北京'}, ...])

            Db.table('user').field('name').field_json('extra', '$.city').select()
            # -> Collection([{'name': '张三', 'city': '北京'}, ...])

        实现要点：

        - JSON 字段走独立通道（``options['json_field']``），在编译期与
          ``field()`` 的结果合并，因此**不会被后续 field() 覆盖**，
          调用顺序自由；
        - ``alias`` 省略时按路径末段自动生成（``$.user.name`` -> ``name``，
          ``$.tags[0]`` -> ``tags``），同名自动加序号；
        - 生成的别名字段自动纳入 JSON 解码列表，无需再调用 ``json()``。
        """
        driver = self._require_json()
        field = self._json_ident(field, "field_json 的列名")
        if alias:
            alias = self._json_alias(alias)
        else:
            alias = self._json_alias_for(field, path)
        used = {a for (_, _, a) in self.options["json_field"]}
        if alias in used:
            i = 2
            while f"{alias}_{i}" in used:
                i += 1
            alias = f"{alias}_{i}"
        self.options["json_field"].append((field, path, alias))
        self._add_json_field(alias)
        return self

    @staticmethod
    def _json_alias_for(field: str, path: Any) -> str:
        """按 JSON 路径最后一段键名生成字段别名。

        取的是最后一个 ``.key`` / ``."key"`` 段，因此 ``$.tags[0]`` 也能
        得到 ``tags``；末段不是合法标识符（如 ``$``、``$."a b"``）时
        回退为列名，避免生成无法用作 SQL 别名的键。
        """
        norm = normalize_json_path(path)
        segs = re.findall(r'\.([A-Za-z_][A-Za-z0-9_]*)|\.\s*"([^"]*)"', norm)
        seg = (segs[-1][0] or segs[-1][1]) if segs else ""
        if seg and _JSON_ALIAS_RE.match(seg):
            return seg
        return field

    def order_json(self, field: str, path: Any = None,
                   direction: str = "asc") -> "Query":
        """按 JSON 路径值排序::

            Db.table('user').order_json('extra', '$.score', 'desc').select()

        对应 ``ORDER BY json_extract(`extra`, '$.score') DESC``；多次调用
        为覆盖语义（与 :meth:`order_raw` 一致）。
        """
        driver = self._require_json()
        field = self._json_ident(field, "order_json 的列名")
        d = str(direction or "asc").strip().upper()
        if d not in ("ASC", "DESC"):
            raise InvalidArgumentException(
                f"order_json 的排序方向只能是 ASC/DESC，收到 {direction!r}")
        expr = driver.json_extract(driver.quote_identifier(field), path)
        return self.order_raw(f"{expr} {d}")

    # ---- JSON 条件内部实现 ---- #
    def _json_where(self, logic: str, field: str, path: Any,
                    op: Any, value: Any) -> "Query":
        """JSON 条件统一入口：参数归一化后追加到 where 列表。

        兼容两种签名（见 :meth:`where_json` 文档）：

        - 显式传了 ``value``：``op`` 必须是合法运算符，否则报错，
          避免拼写错误被静默当成"等于某字符串"
        - 未传 ``value``（哨兵）：``op`` 是运算符时按运算符处理
          （``exists`` / ``is null`` 等），否则视为比较值
        """
        self._require_json()
        field = self._json_ident(field, "JSON 条件的列名")

        if value is _UNSET:
            if isinstance(op, str) and op.strip().lower() in JSON_OPS:
                value = None
            else:
                value, op = op, "="
        elif not (isinstance(op, str) and op.strip().lower() in JSON_OPS):
            raise QueryError(
                f"不支持的 JSON 条件运算符: {op!r}。可选: "
                + ", ".join(sorted(JSON_OPS)))

        op = str(op or "=").strip().lower()
        # None 值自动转为 IS NULL / IS NOT NULL，贴合 SQL 直觉
        if value is None:
            if op == "=":
                op = "is null"
            elif op in ("!=", "<>"):
                op = "is not null"
        self.options["where"].append((logic, JsonWhere(field, path, op, value)))
        return self

    @staticmethod
    def _json_ident(name: Any, what: str) -> str:
        """校验列名/字段名为合法标识符（防注入的最后一道闸口）。"""
        s = str(name).strip()
        if not _JSON_IDENT_RE.match(s):
            raise InvalidArgumentException(
                f"{what} 必须是合法标识符（字母/数字/下划线，可含一层表限定），"
                f"收到 {name!r}")
        return s

    @staticmethod
    def _json_alias(alias: Any) -> str:
        """校验输出别名，确保可安全用于 ``AS`` 与结果字典键。"""
        s = str(alias).strip()
        if not _JSON_ALIAS_RE.match(s):
            raise InvalidArgumentException(
                f"JSON 字段别名必须是合法标识符（字母/数字/下划线），"
                f"收到 {alias!r}")
        return s

    def _require_json(self) -> Any:
        """校验当前驱动是否支持 JSON 路径查询，返回驱动实例。"""
        driver = self._resolve_conn().driver
        if not getattr(driver, "supports_json", False):
            raise UnsupportedOperation(
                f"{driver.name} 驱动不支持 JSON 路径查询"
                f"（supports_json=False）")
        return driver

    def _add_json_field(self, field: str) -> None:
        """把字段登记到自动解码列表（自动模式下无需登记）。"""
        spec = self.options["json"]
        if spec is True:
            return
        if not spec:
            self.options["json"] = [field]
        elif isinstance(spec, list) and field not in spec:
            spec.append(field)

    def with_attr(self, field: str, callback: Callable[[Any], Any]) -> "Query":
        """字段获取器（查询结果转换）。"""
        self.options["attr"][field] = callback
        return self

    def filter(self, callback: Callable[[dict], dict]) -> "Query":
        """结果行过滤/加工回调。"""
        self.options["filter"].append(callback)
        return self

    def allow_empty(self) -> "Query":
        """find 无结果时返回空 dict 而非 None。"""
        self.options["allow_empty"] = True
        return self

    def fail_exception(self) -> "Query":
        """find/select 无结果时抛 DataNotFound。"""
        self.options["fail_exception"] = True
        return self

    def with_(self, *relations: Any) -> "Query":
        """预载入关联：with_('posts') / with_('posts.comments') / with_({'posts': closure})。"""
        for rel in relations:
            if isinstance(rel, dict):
                self.options["with"].append(rel)
            elif isinstance(rel, (list, tuple)):
                self.options["with"].extend(rel)
            else:
                self.options["with"].append(rel)
        return self

    def cache(self, expire: Union[int, float, bool, str] = 60,
              key: Optional[Union[bool, str]] = None) -> "Query":
        """启用查询结果缓存（进程级共享，标准库实现）。

        TTL 语义 —— 第一个参数是"倒计时秒数"，超过即失效::

            Db.name('data').where({'url': url}).cache(10).find()   # 10 秒内复用
            Db.table('user').where('id', 1).cache(30).select()

        兼容旧的 ``cache(key, expire)`` 写法（第一个参数为 ``str``/``bool``
        时自动识别为缓存键）::

            q.cache(60)              # 60 秒 TTL（推荐）
            q.cache('user:1', 120)   # 自定义键 + 120 秒 TTL
            q.cache(120, 'user:1')   # 同上，新写法

        注意事项：

        - 缓存的是查询返回的**原始行**，命中后仍会重新应用 ``json`` /
          ``with_attr`` / ``filter`` / 模型转换，行为与未命中时一致。
        - 写操作（insert / insert_all / update / delete）会自动清除同一库
          同一表的缓存；裸 SQL（``Db.execute``）不会，需手动调用
          ``Db.clear_cache()``。
        - ``expire <= 0`` 视为不缓存（每次穿透到数据库）。
        """
        if isinstance(expire, (bool, str)):
            # 旧签名 cache(key, expire)：第一个位置参数是键
            expire, key = (key if key is not None else 60), expire
        ttl = float(expire or 0)
        if ttl < 0:
            raise InvalidArgumentException("cache() 的 TTL 不能为负数")
        self.options["cache"] = (key if key is not None else True, ttl)
        return self

    def fetch_sql(self, flag: bool = True) -> "Query":
        """仅返回 SQL 不执行。"""
        self.options["fetch_sql"] = flag
        return self

    def copy(self) -> "Query":
        """复制查询对象（便于复用不串条件）。"""
        import copy as _copy
        q = Query(self.conn, prefix=self.prefix, model=self.model)
        q.options = _copy.deepcopy(self.options)
        return q

    # ------------------------------------------------------------------ #
    # 终端方法：读取
    # ------------------------------------------------------------------ #
    def _resolve_conn(self) -> Connection:
        """解析连接。未显式给出连接对象时，回退到全局默认连接。

        v0.3.1 起配置入口收敛回 ``Db.set_config``（连接在构造时即绑定），
        此处仅为 ``Query(conn=None)`` 的兜底。
        """
        if self.conn is None:
            from .connection import get_default_connection
            self.conn = get_default_connection()
            self.builder = Builder(self.conn)
        return self.conn

    def _resolved_prefix(self) -> str:
        """解析表前缀（未显式给出时为空字符串）。"""
        return self.prefix or ""

    def _apply_soft_delete(self) -> None:
        """模型软删除：自动追加 delete_time IS NULL（仅一次）。"""
        sd = self.options.get("soft_delete")
        if sd and not getattr(self, "_soft_applied", False):
            self.options["where"].append(("AND", (sd, "is", None)))
            self._soft_applied = True

    def _execute_select(self) -> Tuple[str, list]:
        self._apply_soft_delete()
        conn = self._resolve_conn()
        sql, params = self.builder.select(self.options)
        self._last_sql, self._last_params = sql, params
        return sql, params

    def select(self) -> Any:
        """查询多条记录。返回 Collection；绑定模型时返回模型实例集合。"""
        sql, params = self._execute_select()

        if self.options["fetch_sql"]:
            return conn_render(sql, params)

        cache_key, ttl = self._resolve_cache_key(sql, params)
        if cache_key:
            hit, cached = _cache.store().get(cache_key)
            if hit:
                # _build_result 内部逐行拷贝（_process_row），不会污染缓存
                result = self._build_result(cached, sql=False)
                if self.options["fail_exception"] and len(result) == 0:
                    raise DataNotFound("查询无结果")
                return result

        rows = self._resolve_conn().query(sql, params)
        if cache_key:
            _cache.store().set(cache_key, rows, ttl)
        result = self._build_result(rows)
        if self.options["fail_exception"] and len(result) == 0:
            raise DataNotFound("查询无结果")
        return result

    def find(self, id: Optional[Any] = None) -> Any:
        """查询单条记录。``find(1)`` 按主键查询（think-orm 语义）。

        返回 dict/模型实例；无结果返回 None（allow_empty 时返回空对象，
        fail_exception 时抛 DataNotFound）。
        """
        if id is not None:
            pk = self._pk_name()
            self.where(pk, id)
        self.options["limit"] = 1
        sql, params = self._execute_select()

        if self.options["fetch_sql"]:
            return conn_render(sql, params)

        rows: Optional[List[dict]] = None
        cache_key, ttl = self._resolve_cache_key(sql, params)
        if cache_key:
            hit, cached = _cache.store().get(cache_key)
            if hit:
                rows = [dict(r) for r in cached]
        if rows is None:
            rows = self._resolve_conn().query(sql, params)
            if cache_key:
                # 写入副本：find 的快路径会把 rows[0] 直接返回给调用方，
                # 若缓存与返回值共享同一 dict，调用方的修改会污染缓存。
                _cache.store().set(cache_key, [dict(r) for r in rows], ttl)

        if not rows:
            if self.options["fail_exception"]:
                raise DataNotFound("查询无结果")
            if self.options["allow_empty"]:
                if self.model is not None:
                    return self.model._from_data({})
                return {}
            return None
        # 快路径：纯表查询（无模型绑定、无 json/attr/filter/with 后处理），
        # 直接返回首行，跳过 Collection 包装与结果二次拷贝。
        # 缓存命中时 rows 已逐行拷贝，直接返回 rows[0] 不会污染缓存。
        o = self.options
        if self.model is None and not (o["json"] or o["attr"] or o["filter"] or o["with"]):
            return rows[0]
        return self._build_result(rows, sql=False)[0]

    def find_or_empty(self, id: Optional[Any] = None) -> Any:
        return self.allow_empty().find(id)

    def find_or_fail(self, id: Optional[Any] = None) -> Any:
        return self.fail_exception().find(id)

    def select_or_fail(self) -> Any:
        result = self.select()
        if len(result) == 0:
            raise DataNotFound("查询无结果")
        return result

    def chunk(self, size: int = 1000) -> Iterator[Any]:
        """分块迭代查询结果（大批量处理时避免整体载入内存）。

        每批独立执行一次 ``LIMIT`` 查询，每批都受连接锁保护（线程安全）；
        代价是跨批之间数据可能变化——需要强一致时请在外层包
        ``Db.transaction()``。建议配合 ``order()``，否则分批顺序不稳定::

            for batch in Db.table('log').order('id').chunk(500):
                for row in batch:
                    ...

        每批产出 ``Collection``（绑定模型时为模型集合），与 ``select()`` 一致。
        """
        if size <= 0:
            raise InvalidArgumentException("chunk() 的 size 必须大于 0")
        base = self.copy()
        base.options["limit"] = None
        offset = 0
        while True:
            q = base.copy()
            q.options["limit"] = [size, offset]
            batch = q.select()
            if not len(batch):
                break
            yield batch
            if len(batch) < size:
                break
            offset += size

    def cursor(self, chunk_size: int = 1000) -> Iterator[Any]:
        """流式逐行读取（底层 ``fetchmany``，内存占用恒定）。

        与 ``chunk()`` 的区别：本方法在整个游标生命周期内不持有连接锁，
        因此效率更高，但同一连接上的并发写入可能影响游标结果，适合
        "只读导出 / 全表扫描" 场景。绑定模型时逐行产出模型实例::

            for row in Db.table('big').order('id').cursor():
                ...

        需要线程安全的批量处理请改用 :meth:`chunk`。
        """
        if chunk_size <= 0:
            raise InvalidArgumentException("cursor() 的 chunk_size 必须大于 0")
        sql, params = self._execute_select()
        if self.options["fetch_sql"]:
            raise QueryError("fetch_sql 模式下无法流式读取")
        conn = self._resolve_conn()
        for raw in conn.query_stream(sql, params, chunk_size):
            if self.model is not None:
                yield self.model._from_data(self._process_row(raw))
            else:
                yield self._process_row(raw)

    def value(self, field: str, default: Any = None) -> Any:
        """取第一条记录的单个字段值。"""
        q = self.field(field).limit(1)
        row = q.find()
        if row is None:
            return default
        if isinstance(row, dict):
            return row.get(field, default)
        return getattr(row, field, default)

    def column(self, field: str, key: Optional[str] = None) -> Any:
        """取某字段值列表；``column('name', 'id')`` 以 id 为键。"""
        q = self.field([field, key] if key else [field])
        rows = q.select()
        values = []
        for r in rows:
            if isinstance(r, dict):
                values.append(r.get(field))
            else:
                values.append(getattr(r, field))
        if key is None:
            return values
        result = {}
        for r, v in zip(rows, values):
            if isinstance(r, dict):
                result[r.get(key)] = v
            else:
                result[getattr(r, key)] = v
        return result

    # ------------------------------------------------------------------ #
    # 终端方法：聚合
    # ------------------------------------------------------------------ #
    def count(self, field: str = "*") -> int:
        return self._aggregate("COUNT", field)

    def sum(self, field: str) -> Union[int, float]:
        return self._aggregate("SUM", field)

    def avg(self, field: str) -> float:
        return self._aggregate("AVG", field)

    def max(self, field: str) -> Any:
        return self._aggregate("MAX", field)

    def min(self, field: str) -> Any:
        return self._aggregate("MIN", field)

    def _aggregate(self, fn: str, field: str) -> Any:
        qi = self._resolve_conn().driver.quote_identifier
        if field == "*":
            expr = "COUNT(*)"
        else:
            expr = f"{fn}({qi(field)})"
        q = self.copy()
        q.options["field"] = Raw(expr)
        q.options["limit"] = None
        sql, params = q._execute_select()
        if q.options["fetch_sql"]:
            return conn_render(sql, params)
        cache_key, ttl = q._resolve_cache_key(sql, params)
        row: Optional[List[dict]] = None
        if cache_key:
            hit, cached = _cache.store().get(cache_key)
            if hit:
                row = [dict(r) for r in cached]
        if row is None:
            row = q._resolve_conn().query(sql, params)
            if cache_key:
                _cache.store().set(cache_key, row, ttl)
        if not row:
            return 0 if fn == "COUNT" else None
        return row[0][expr] if expr in row[0] else list(row[0].values())[0]

    def paginate(self, list_rows: int = 15, page: Optional[int] = None) -> Any:
        """分页查询，返回 Paginator（含 total/items/current_page/last_page）。"""
        from .collection import Paginator
        total = self.count()
        current = page or 1
        if current < 1:
            current = 1
        items = self.copy().page(current, list_rows).select()
        return Paginator(items, total, current, list_rows)

    # ------------------------------------------------------------------ #
    # 终端方法：写入
    # ------------------------------------------------------------------ #
    def insert(self, data: Optional[dict] = None) -> int:
        """插入一条，返回自增主键（无自增返回 1）。"""
        if data is None:
            data = self.options.get("data") or {}
        if not data:
            raise QueryError("insert 数据不能为空")
        conn = self._resolve_conn()
        sql, params = self.builder.insert(self.options, data)
        self._last_sql, self._last_params = sql, params
        if self.options["fetch_sql"]:
            return conn_render(sql, params)
        last_id = conn.insert(sql, params)
        self._invalidate_cache()
        return last_id

    def insert_all(self, data_list: List[dict], batch_size: Optional[int] = None) -> int:
        """批量插入，返回插入行数。

        行数较多时自动分批，规避 SQLite 单语句绑定变量上限
        （SQLITE_MAX_VARIABLE_NUMBER = 32766）：单批上限为 ``32766 // 列数``。
        分批在同一事务内完成以保证原子性；若调用方已处于事务中，
        则退化为 SAVEPOINT 嵌套，语义不变。

        ``batch_size`` 可显式指定每批行数（默认按列数自动计算）。
        """
        if not data_list:
            return 0
        conn = self._resolve_conn()

        fields = list(data_list[0].keys())
        size = batch_size or max(1, MAX_SQL_VARIABLES // max(len(fields), 1))

        # 单批可容纳：保持原有路径，零额外开销
        if len(data_list) <= size:
            sql, params = self.builder.insert_all(self.options, data_list)
            self._last_sql, self._last_params = sql, params
            if self.options["fetch_sql"]:
                return conn_render(sql, params)
            conn.execute(sql, params)
            self._invalidate_cache()
            return len(data_list)

        # 多批：先整体校验字段一致性，保证与单批一致的报错行为
        expected = set(fields)
        for row in data_list:
            if set(row.keys()) != expected:
                raise QueryError("insert_all 各行字段必须一致")

        # fetch_sql 模式不执行，返回首条 SQL 供调试
        if self.options["fetch_sql"]:
            sql, params = self.builder.insert_all(self.options, data_list[:size])
            self._last_sql, self._last_params = sql, params
            return conn_render(sql, params)

        total = 0
        with conn.transaction():
            for i in range(0, len(data_list), size):
                chunk = data_list[i:i + size]
                sql, params = self.builder.insert_all(self.options, chunk)
                self._last_sql, self._last_params = sql, params
                conn.execute(sql, params)
                total += len(chunk)
        self._invalidate_cache()
        return total

    def update(self, data: Optional[dict] = None) -> int:
        """更新数据，返回影响行数。必须存在 where 条件。"""
        if data is None:
            data = self.options.get("data") or {}
        return self._execute_update(data)

    def _execute_update(self, data: dict) -> int:
        """UPDATE 终端执行（普通字段更新与 JSON 路径更新共用）。

        普通字段空但存在 JSON 路径写入规格时同样合法——二者最终拼进同一条
        UPDATE 语句，具体合并由 Builder 负责。
        """
        specs = self.options.get("json_update") or []
        if not data and not specs:
            raise QueryError("update 数据不能为空")
        self._apply_soft_delete()
        conn = self._resolve_conn()
        sql, params = self.builder.update(self.options, data)
        self._last_sql, self._last_params = sql, params
        if self.options["fetch_sql"]:
            return conn_render(sql, params)
        affected = conn.execute(sql, params)
        self._invalidate_cache()
        return affected

    # ------------------------------------------------------------------ #
    # 终端方法：JSON 路径写入（局部更新，v0.6.0）
    # ------------------------------------------------------------------ #
    def update_json(self, field: str, path: Any = None,
                    value: Any = _UNSET, ifnull: Any = None) -> int:
        """按 JSON 路径**局部更新**，返回影响行数。需先指定 where 条件。

        ::

            # 单路径：把 $.age 置为 19
            Db.table('user').where('id', 1).update_json('extra', '$.age', 19)

            # 多路径：一次 SQL 内原子写入（推荐，避免多次往返）
            Db.table('user').where('id', 1).update_json(
                'extra', {'$.age': 19, '$.city': '上海'})

            # 与 JSON 条件组合：给满足条件的记录打标
            Db.table('user').where_json('extra', '$.age', '>', 30).update_json(
                'extra', '$.level', 'gold')

        相比"整对象读出 → Python 改 → 写回"，本方法在 SQL 内完成改写：

        - **并发正确**：不存在"读—改—写"窗口，交错执行不会丢失更新
          （读改写方式下，另一个写入会覆盖前一个的改动）；
        - **少一次往返**：省掉读出整列的那条查询；
        - **省 Python 侧解析**：无需 ``json.loads`` 后再 ``json.dumps``。

        **写入量不变**：JSON 列以文本存储，``json_set`` 同样会重写整列
        文本，写入放大与读改写一致；路径级写入的收益在往返、Python 侧
        开销与并发正确性，不在磁盘写入量。

        参数语义：

        - ``path`` 传 ``dict`` 时按 ``{路径: 值}`` 展开为多路径写入；
        - ``value`` 传 ``None`` 表示写入 JSON ``null``，**不是删除**
          （删除请用 :meth:`update_json_remove`）；
        - 值为 ``dict`` / ``list`` / ``tuple`` 时按 JSON 结构写入，
          其余按标量写入；
        - ``ifnull`` 处理"列为 SQL NULL"的边界：SQLite / MySQL 的
          ``json_set(NULL, ...)`` 返回 NULL（更新静默无效），传 ``{}``
          或 ``[]`` 可让空列先视为空文档。
        """
        return self._json_write("set", field, path, value, ifnull)

    def update_json_insert(self, field: str, path: Any,
                           value: Any = _UNSET,
                           ifnull: Any = None) -> int:
        """按 JSON 路径写入，**仅当路径不存在时**生效（同 ``json_insert``）。

        适合初始化默认字段，不覆盖已有值::

            Db.table('user').where('id', 1).update_json_insert(
                'extra', '$.level', 'normal')
        """
        return self._json_write("insert", field, path, value, ifnull)

    def update_json_remove(self, field: str, path: Any,
                           ifnull: Any = None) -> int:
        """删除 JSON 路径（可多个），路径不存在时静默忽略。

        ::

            Db.table('user').where('id', 1).update_json_remove('extra', '$.tmp')
            Db.table('user').where('id', 1).update_json_remove(
                'extra', ['$.tmp', '$.cache'])
        """
        return self._json_write("remove", field, path, _UNSET, ifnull)

    def update_json_patch(self, field: str, patch: dict,
                          ifnull: Any = None) -> int:
        """按 RFC 7396 JSON Merge Patch 合并文档，返回影响行数。

        ``patch`` 为补丁文档：对象**递归合并**，数组**整体替换**，
        值为 ``null`` 表示**删除该键**::

            Db.table('user').where('id', 1).update_json_patch(
                'extra', {'level': 'gold', 'tmp': None})   # 同时写 level、删 tmp

        **语义对比**：本方法里补丁的 ``null`` 是"删除键"，而
        :meth:`update_json` 的值 ``null`` 是"置为 JSON null"，二者相反。
        """
        return self._json_write("patch", field, patch, _UNSET, ifnull)

    def update_json_ops(self, field: str, ops: Sequence[Any],
                        ifnull: Any = None) -> int:
        """在**一条** UPDATE 语句内按序应用多个 JSON 路径操作。

        上面每个 ``update_json_*`` 方法一次只表达一种操作；本方法接受操作
        列表，把 ``set`` / ``insert`` / ``remove`` / ``patch`` 混合编译进
        同一条语句，按列表顺序依次生效::

            Db.table('user').where('id', 1).update_json_ops('extra', [
                ('set',    '$.age', 19),        # 改值
                ('remove', '$.tmp'),            # 删键
                ('insert', '$.level', 'vip'),   # 不存在才写
                ('patch',  {'meta': {'v': 2}}), # RFC 7396 合并
                ('remove', ['$.a', '$.b']),     # 一次删多个
            ])

        操作元素支持五种写法（模式取 ``JSON_UPDATE_MODES``，大小写不敏感）：

        1. ``(模式, 路径, 值)`` —— ``set`` / ``insert`` 用这种三元组；
        2. ``(模式, 路径)`` —— ``remove`` 用（删除无需值）；``路径`` 传
           ``list`` 可一次删除多个；
        3. ``(模式, 补丁文档)`` —— ``patch`` 用（补丁自带键路径，无路径元素）；
        4. ``{路径: 值}`` —— ``set`` 多路径简写，等价于
           ``update_json(field, {...})``；
        5. :class:`~tinkpyorm.builder.JsonUpdate` 实例 —— 高级用法，可自带
           列名，因而能在**一条语句内改多个 JSON 列**。

        顺序语义（已实测确认）：

        - **列表顺序即应用顺序**：``[('set','$.a',1), ('remove','$.a')]``
          最终删除 ``$.a``；颠倒两者顺序则保留为 ``1``；
        - 相邻同类操作会被合并进同一次函数调用
          （``json_set(col, p1, v1, p2, v2)``）；SQLite 对同一次调用内的
          重复路径同样**后者胜**，故合并不改变语义；
        - ``patch`` 的 ``null`` 语义是"删除该键"，与 :meth:`update_json`
          的值 ``null``（置为 JSON null）相反。

        非法模式、缺失值、``patch`` 非 ``dict``、``remove`` 带值等均在
        构造阶段抛 ``InvalidArgumentException``，不会生成半成品 SQL。
        """
        self._require_json()
        column = self._json_ident(field, "JSON 路径更新的列名")
        self._json_ifnull(ifnull)
        if not isinstance(ops, (list, tuple)):
            raise InvalidArgumentException(
                "update_json_ops 需要操作列表（list / tuple），"
                f"收到 {type(ops).__name__}")
        if not ops:
            raise InvalidArgumentException("update_json_ops 的操作列表不能为空")
        # 先全量解析校验，通过后再写入状态：避免中途失败留下半成品规格
        pending: List[JsonUpdate] = []
        for op in ops:
            pending.extend(self._json_op_specs(column, op, ifnull))
        self.options.setdefault("json_update", []).extend(pending)
        return self._execute_update(self.options.get("data") or {})

    @classmethod
    def _json_op_specs(cls, column: str, op: Any,
                       ifnull: Any) -> List[JsonUpdate]:
        """把单个操作元素解析为若干 :class:`JsonUpdate` 规格。

        与 :meth:`_json_write` 共用同一套语义校验，保证两个入口行为一致：
        校验都在构造阶段完成，Builder 只负责纯编译。
        """
        # 4) JsonUpdate 实例：直接采用（自带列名，可跨列）
        if isinstance(op, JsonUpdate):
            if op.mode not in JSON_UPDATE_MODES:
                raise InvalidArgumentException(
                    f"不支持的 JSON 写入模式: {op.mode!r}。可选: "
                    + ", ".join(JSON_UPDATE_MODES))
            return [op]

        # 3) {路径: 值} 简写 -> set 多路径
        if isinstance(op, dict):
            if not op:
                raise InvalidArgumentException(
                    "JSON 路径映射不能为空（形如 {'$.a': 1}）")
            return [JsonUpdate(column, p, v, "set", ifnull)
                    for p, v in op.items()]

        # 1) / 2) 元组形式
        if not isinstance(op, (list, tuple)):
            raise InvalidArgumentException(
                "update_json_ops 的每个操作须为 (模式, 路径[, 值]) 元组、"
                f"{{路径: 值}} 映射或 JsonUpdate 实例；收到 {type(op).__name__}")
        if len(op) < 2:
            raise InvalidArgumentException(
                f"操作 {op!r} 至少需要 (模式, 路径) 两个元素")

        mode = op[0]
        if not (isinstance(mode, str)
                and mode.strip().lower() in JSON_UPDATE_MODES):
            raise InvalidArgumentException(
                f"不支持的 JSON 写入模式: {mode!r}。可选: "
                + ", ".join(JSON_UPDATE_MODES))
        mode = mode.strip().lower()
        path = op[1]

        if mode == "remove":
            if len(op) > 2:
                raise InvalidArgumentException(
                    "remove 操作不接受值（删除无需值）")
            paths = list(path) if isinstance(path, (list, tuple)) else [path]
            if not paths:
                raise InvalidArgumentException("remove 操作至少需要一个路径")
            return [JsonUpdate(column, p, None, "remove", ifnull)
                    for p in paths]

        if mode == "patch":
            # 补丁自带键路径，故只需 (模式, 补丁文档)，无路径元素
            if len(op) != 2:
                raise InvalidArgumentException(
                    "patch 操作的写法为 (模式, 补丁文档)；补丁自带键路径，"
                    "不要再提供路径元素")
            patch = op[1]
            if not isinstance(patch, dict):
                raise InvalidArgumentException(
                    "patch 操作的补丁必须是 dict（RFC 7396 文档）")
            return [JsonUpdate(column, None, patch, "patch", ifnull)]

        if len(op) < 3:
            raise InvalidArgumentException(
                f"{mode} 操作需要提供值，写法为 (模式, 路径, 值)")

        value = op[2]
        if isinstance(path, dict):
            raise InvalidArgumentException(
                "set / insert 的路径不应是 dict；多路径请直接以 "
                "{'$.a': 1, '$.b': 2} 作为一个操作元素，或拆成多条操作")
        return [JsonUpdate(column, path, value, mode, ifnull)]

    def _json_write(self, mode: str, field: str, path: Any,
                    value: Any, ifnull: Any) -> int:
        """JSON 路径写入族统一入口：归一化参数后追加规格并执行 UPDATE。"""
        self._require_json()
        column = self._json_ident(field, "JSON 路径更新的列名")
        self._json_ifnull(ifnull)
        specs = self.options.setdefault("json_update", [])

        if mode == "remove":
            if value is not _UNSET:
                raise InvalidArgumentException(
                    "update_json_remove 不接受值参数（删除无需值）")
            paths = list(path) if isinstance(path, (list, tuple)) else [path]
            if not paths:
                raise InvalidArgumentException(
                    "update_json_remove 至少需要一个路径")
            for p in paths:
                specs.append(JsonUpdate(column, p, None, "remove", ifnull))
        elif mode == "patch":
            if not isinstance(path, dict):
                raise InvalidArgumentException(
                    "update_json_patch 的补丁必须是 dict（RFC 7396 文档）")
            specs.append(JsonUpdate(column, None, path, "patch", ifnull))
        elif isinstance(path, dict):
            # 多路径写法 {路径: 值}
            if value is not _UNSET:
                raise InvalidArgumentException(
                    "path 已用 dict 指定多路径，不应再传 value"
                    "（值请写在 dict 内：{'$.a': 1, '$.b': 2}）")
            if not path:
                raise InvalidArgumentException("JSON 路径映射不能为空")
            for p, v in path.items():
                specs.append(JsonUpdate(column, p, v, mode, ifnull))
        else:
            if value is _UNSET:
                raise InvalidArgumentException(
                    "update_json 需要提供值；仅删除路径请用 "
                    "update_json_remove()")
            specs.append(JsonUpdate(column, path, value, mode, ifnull))

        return self._execute_update(self.options.get("data") or {})

    @staticmethod
    def _json_ifnull(ifnull: Any) -> None:
        """校验"空列兜底"取值：只允许空 ``dict`` / 空 ``list``。

        兜底文档由驱动编译为**常量字面量**（``'{}'`` / ``'[]'``），不接受
        任意字面量，从机制上避免把用户输入拼进 SQL。
        """
        if ifnull is None:
            return
        if isinstance(ifnull, dict) and not ifnull:
            return
        if isinstance(ifnull, (list, tuple)) and not ifnull:
            return
        raise InvalidArgumentException(
            "ifnull 只接受空 dict（视为空对象）或空 list（视为空数组）；"
            f"收到 {ifnull!r}")

    def delete(self, id: Optional[Any] = None) -> int:
        """删除数据，返回影响行数。``delete(1)`` 按主键删除。"""
        if id is not None:
            pk = self._pk_name()
            self.where(pk, id)
        conn = self._resolve_conn()
        sql, params = self.builder.delete(self.options)
        self._last_sql, self._last_params = sql, params
        if self.options["fetch_sql"]:
            return conn_render(sql, params)
        affected = conn.execute(sql, params)
        self._invalidate_cache()
        return affected

    def save(self, data: Optional[dict] = None) -> int:
        """有主键值则更新，否则插入（think-orm Query::save 语义）。"""
        data = dict(data or self.options.get("data") or {})
        pk = self._pk_name()
        if pk in data and data[pk] is not None:
            pk_val = data.pop(pk)
            return self.where(pk, pk_val).update(data)
        return self.insert(data)

    def inc(self, field: str, step: int = 1) -> int:
        """字段自增。"""
        qi = self._resolve_conn().driver.quote_identifier
        return self.update({field: raw(f"{qi(field)} + {int(step)}")})

    def dec(self, field: str, step: int = 1) -> int:
        """字段自减。"""
        qi = self._resolve_conn().driver.quote_identifier
        return self.update({field: raw(f"{qi(field)} - {int(step)}")})

    def get_last_sql(self) -> str:
        return conn_render(self._last_sql, self._last_params)

    # ------------------------------------------------------------------ #
    # camelCase 别名（think-orm 文档风格: whereOr / whereNull / findOrFail ...）
    # ------------------------------------------------------------------ #
    def __getattr__(self, name: str) -> Any:
        snake = to_snake(name)
        if snake != name and hasattr(self.__class__, snake):
            return getattr(self, snake)
        # 模型查询范围：在已绑定模型的 Query 上可直接调用 scope_xxx 并继续链式串联
        # 例：User.where('name', 'like', '%a%').active().select()
        # 支持两种定义风格（与 MetaModel 一致）：
        #   @classmethod def scope_active(cls, query)   或  def scope_active(self, query)
        model = self.__dict__.get("model")
        if model is not None:
            for scope_name in (f"scope_{snake}", f"scope_{name}"):
                raw_scope = None
                for c in getattr(model, "__mro__", ()):
                    if scope_name in c.__dict__:
                        raw_scope = c.__dict__[scope_name]
                        break
                if raw_scope is None:
                    continue
                if isinstance(raw_scope, classmethod):
                    bound_scope = getattr(model, scope_name)

                    def scope_applier(*args: Any, **kwargs: Any) -> "Query":
                        bound_scope(self, *args, **kwargs)
                        return self
                else:
                    def scope_applier(*args: Any, **kwargs: Any) -> "Query":
                        raw_scope(None, self, *args, **kwargs)
                        return self

                scope_applier.__name__ = name
                return scope_applier
        raise AttributeError(f"'Query' object has no attribute {name!r}")

    def explain(self) -> List[dict]:
        sql, params = self._execute_select()
        return self._resolve_conn().query(f"EXPLAIN QUERY PLAN {sql}", params)

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    def _pk_name(self) -> str:
        if self.model is not None:
            return self.model.__pk__
        return "id"

    def _cache_spec(self) -> Tuple[Optional[str], float]:
        """解析缓存配置，返回 ``(键, TTL 秒)``。

        键为 ``None`` 表示未启用缓存（或 TTL <= 0，即不缓存）；
        返回空串表示"按 SQL 与参数自动生成键"。
        """
        spec = self.options.get("cache")
        if not spec:
            return None, 0.0
        key, ttl = spec
        ttl = float(ttl or 0)
        if ttl <= 0:
            return None, 0.0
        if isinstance(key, str) and key:
            return key, ttl
        return "", ttl

    def _cache_table(self) -> str:
        """缓存键使用的表名（取首个表，忽略别名）。"""
        tables = self.options.get("table") or []
        if tables and isinstance(tables[0], dict):
            return tables[0].get("name") or ""
        return ""

    def _cache_database(self) -> str:
        """缓存键使用的库标识（未绑定连接时为空串）。"""
        if self.conn is None:
            return ""
        return getattr(self.conn.config, "database", "") or ""

    def _resolve_cache_key(self, sql: str, params: Sequence[Any]) -> Tuple[Optional[str], float]:
        """生成最终缓存键。

        自定义键同样带上"库 + 表"前缀，使写操作能按前缀统一失效。
        """
        key, ttl = self._cache_spec()
        if key is None:
            return None, 0.0
        if key == "":
            return _cache.make_key(self._cache_database(),
                                   self._cache_table(), sql, params), ttl
        prefix = _cache.prefix_for(self._cache_database(), self._cache_table())
        return prefix + "user:" + key, ttl

    def _invalidate_cache(self) -> int:
        """写操作后清除同一库同一表的全部缓存，返回清除条数。"""
        prefix = _cache.prefix_for(self._cache_database(), self._cache_table())
        return _cache.clear(prefix)

    def _process_row(self, row: Any) -> dict:
        """单行后处理：JSON 解码 -> 获取器（attr）-> filter 回调。

        JSON 解码交由驱动（``driver.json_decode``）：SQLite / MySQL 解析
        JSON 文本，MongoDB / Redis 恒等透传。解码幂等，``None`` 直接跳过。
        """
        item = dict(row)
        spec = self.options["json"]
        if spec:
            decode = (self.conn.driver.json_decode if self.conn is not None
                      else _plain_json_decode)
            fields = list(item.keys()) if spec is True else spec
            for f in fields:
                if f in item and item[f] is not None:
                    item[f] = decode(item[f])
        for f, cb in self.options["attr"].items():
            if f in item:
                item[f] = cb(item[f])
        for cb in self.options["filter"]:
            item = cb(item)
        return item

    def _build_result(self, rows: List[dict], sql: bool = True) -> Any:
        """原始行 -> 应用 json/attr/filter -> (模型实例) 结果集。"""
        from .collection import Collection

        results = [self._process_row(row) for row in rows]

        if self.model is not None:
            models = [self.model._from_data(r) for r in results]
            if self.options["with"]:
                self._eager_load(models)
            return Collection(models, model=self.model)
        return Collection(results)

    def _eager_load(self, models: list) -> None:
        """预载入关联（支持嵌套 'posts.comments' 与闭包约束）。"""
        for spec in self.options["with"]:
            if isinstance(spec, dict):
                for name, closure in spec.items():
                    self._eager_one(models, name, closure)
            elif isinstance(spec, str):
                parts = spec.split(".")
                self._eager_one(models, parts[0], None, parts[1:] if len(parts) > 1 else None)
            else:
                self._eager_one(models, str(spec), None)

    def _eager_one(self, models: list, name: str, closure: Optional[Callable] = None, nested: Optional[list] = None) -> None:
        if not models:
            return
        cls = type(models[0])
        relation = cls._get_relation(name, models[0])
        relation.eagerly(models, closure)
        if nested:
            # 递归加载嵌套关联（如 'posts.comments'）
            from .collection import Collection
            related = []
            for m in models:
                rel_data = m._relation.get(name)
                if rel_data is None:
                    continue
                if isinstance(rel_data, (list, Collection)):
                    related.extend(rel_data)
                else:
                    related.append(rel_data)
            if related:
                tmp = Query(self.conn, model=type(related[0]))
                tmp.options["with"] = [".".join(nested)]
                tmp._eager_load(related)


def _plain_json_decode(value: Any) -> Any:
    """无连接上下文时的 JSON 解码兜底（正常路径走 driver.json_decode）。"""
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return value
    return value


def conn_render(sql: str, params: Sequence[Any]) -> str:
    """把 SQL 与参数渲染为可读字符串（仅供调试，不用于执行）。"""
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
