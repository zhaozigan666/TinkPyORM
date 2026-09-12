"""SQL 构造器（对应 think-orm 的 BaseBuilder/Builder）。

将 Query 的 options 编译为 SQL 与绑定参数，所有用户值一律走 ``?``
参数绑定，标识符经 quote_ident 引用，从机制上杜绝 SQL 注入。
"""
from __future__ import annotations

import json
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .connection import Connection
from .drivers import UnsupportedOperation, json_path_literal
from .exceptions import QueryError
from .utils import Raw, parse_field, parse_order, is_raw

__all__ = ["Builder", "RawWhere", "JsonWhere", "JsonUpdate",
           "JSON_OPS", "JSON_UPDATE_MODES"]

# 支持的比较运算符
_OPS = {
    "=", "!=", "<>", ">", "<", ">=", "<=",
    "like", "not like", "in", "not in", "between", "not between",
    "is", "is not", "exists", "not exists", "find_in_set", "exp", "raw",
}

# JSON 路径条件支持的运算符（与 _OPS 分离，语义为"作用于 JSON 路径值"）
JSON_OPS = {
    "=", "!=", "<>", ">", "<", ">=", "<=",
    "like", "not like", "in", "not in", "between", "not between",
    "is null", "is not null", "null", "not null",
    "exists", "not exists", "contains", "not contains",
}

# JSON 路径写入的操作类型（与查询侧 JSON_OPS 对称）
JSON_UPDATE_MODES = ("set", "insert", "remove", "patch")

_JOIN_TYPES = {"INNER", "LEFT", "RIGHT", "FULL", "CROSS"}


class JsonWhere:
    """JSON 路径条件（由 ``Query.where_json()`` 构造）。

    独立于普通字段条件存在，是因为 JSON 条件的 SQL 形态由**驱动方言**
    决定（SQLite 用 ``json_extract``，MySQL 用 ``JSON_EXTRACT``，
    MongoDB 走原生查询），交给 Builder 在编译期向驱动取表达式，
    从而避免把方言知识泄露到 Query 层。

    参数说明：

    - ``column``：JSON 列名
    - ``path``：JSON 路径（``$.a.b`` / ``a.b`` / ``[0]``，见 normalize_json_path）
    - ``op``：运算符（见 ``JSON_OPS``）
    - ``value``：比较值，走参数绑定
    - ``func``：左值取用哪个驱动函数 ——
      ``None`` 为路径值提取（``json_extract``），
      ``'length'`` 为元素个数（``json_length``），
      ``'type'`` 为类型名（``json_type``）

    参数 ``path`` 由驱动校验后内联（见 ``normalize_json_path``），
    ``value`` 一律绑定参数。
    """

    __slots__ = ("column", "path", "op", "value", "func")

    def __init__(self, column: str, path: Any = None,
                 op: str = "=", value: Any = None,
                 func: Optional[str] = None):
        self.column = column
        self.path = path
        self.op = op
        self.value = value
        self.func = func

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"JsonWhere({self.column!r}, {self.path!r}, "
                f"{self.op!r}, {self.value!r}, func={self.func!r})")


class JsonUpdate:
    """JSON 路径写入规格（由 ``Query.update_json()`` 族构造）。

    与 :class:`JsonWhere` 对称：查询条件由驱动编译为 ``json_extract``，
    路径写入由驱动编译为 ``json_set`` 族。Query 层只描述"改哪个路径、
    写成什么值"，方言知识全部留在驱动层。

    参数说明：

    - ``column``：JSON 列名（已由 Query 校验为合法标识符）
    - ``path``：JSON 路径；``patch`` 模式无路径（补丁自带键路径）
    - ``value``：写入值，一律参数绑定；``patch`` 模式为补丁文档
    - ``mode``：``set`` / ``insert`` / ``remove`` / ``patch``
    - ``ifnull``：列为 SQL NULL 时的兜底空文档（``{}`` 或 ``[]``）

    同一列的多个规格由 Builder 合并进**同一次** JSON 函数调用：SQL 中
    同一列出现多个 SET 子句时只有最后一个生效，拆开会导致静默丢更新。
    """

    __slots__ = ("column", "path", "value", "mode", "ifnull")

    def __init__(self, column: str, path: Any = None, value: Any = None,
                 mode: str = "set", ifnull: Any = None):
        self.column = column
        self.path = path
        self.value = value
        self.mode = mode
        self.ifnull = ifnull

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"JsonUpdate({self.column!r}, {self.path!r}, "
                f"{self.value!r}, mode={self.mode!r})")


class RawWhere:
    """带参数绑定的原生 WHERE 片段（where_raw 使用）。

    与 Raw 的区别：Raw 的值不绑定参数；RawWhere 支持 ``?`` 占位符
    并携带对应参数。
    """

    __slots__ = ("sql", "params")

    def __init__(self, sql: str, params: Sequence[Any] = ()):
        self.sql = sql
        self.params = list(params)


class Builder:
    """针对 SQLite 的 SQL 构造器。"""

    def __init__(self, conn: Connection):
        self.conn = conn
        # 标识符引用统一走驱动方言（SQLite/PostgreSQL/MSSQL 各异）
        self._qi = conn.driver.quote_identifier

    # ------------------------------------------------------------------ #
    # SELECT
    # ------------------------------------------------------------------ #
    def select(self, options: dict) -> Tuple[str, list]:
        sql_parts = ["SELECT"]
        if options.get("distinct"):
            sql_parts.append("DISTINCT")
        if options.get("comment"):
            sql_parts.append(f"/* {options['comment']} */")

        fields = self._build_field(options)
        sql_parts.append(fields)
        sql_parts.append("FROM")
        sql_parts.append(self._build_table(options))

        join_sql, join_params = self._build_join(options)
        if join_sql:
            sql_parts.append(join_sql)

        where_sql, where_params = self.build_where(options.get("where", []))
        if where_sql:
            sql_parts.append(where_sql)

        group = self._build_group(options)
        if group:
            sql_parts.append(group)

        having_sql, having_params = self._build_having(options)
        if having_sql:
            sql_parts.append(having_sql)

        union_sql, union_params = self._build_union(options)
        if union_sql:
            sql_parts.append(union_sql)

        order = self._build_order(options)
        if order:
            sql_parts.append(order)

        limit = self._build_limit(options)
        if limit:
            sql_parts.append(limit)

        if options.get("lock"):
            sql_parts.append("FOR UPDATE" if options["lock"] is True else str(options["lock"]))

        sql = " ".join(sql_parts)
        return sql, join_params + where_params + having_params + union_params

    def _build_table(self, options: dict) -> str:
        table = options.get("table")
        if not table:
            raise QueryError("查询未指定数据表（table），请使用 table()/name() 或模型")
        parts = []
        for t in table:
            alias = t.get("alias")
            name = t["name"]
            if "." in name:
                name = ".".join(self._qi(x) for x in name.split("."))
            else:
                name = self._qi(name)
            parts.append(f"{name} {self._qi(alias)}" if alias else name)
        return ", ".join(parts)

    def _build_field(self, options: dict) -> str:
        fields = options.get("field")
        without = options.get("without_field")
        json_fields = options.get("json_field") or []

        if fields:
            base = ", ".join(parse_field(fields, self._qi))
        elif without:
            # SQLite 不支持 SELECT * EXCEPT：基于表结构生成排除后的字段列表
            tables = options.get("table") or []
            if len(tables) != 1:
                raise QueryError("without_field 仅支持单表查询")
            table = tables[0]["name"]
            if table.startswith("("):  # 子查询场景不处理
                raise QueryError("without_field 不支持子查询表")
            try:
                all_fields = [r["name"] for r in self.conn.query(f"PRAGMA table_info({self._qi(table)})")]
            except Exception:
                raise QueryError("without_field 需要表已存在以读取表结构")
            exclude = set(without) if isinstance(without, (list, tuple, set)) else {f.strip() for f in str(without).split(",") if f.strip()}
            keep = [f for f in all_fields if f not in exclude]
            if not keep:
                raise QueryError("without_field 排除后无可用字段")
            base = ", ".join(self._qi(f) for f in keep)
        else:
            base = "*"

        # JSON 路径字段独立通道，编译期合并（不受 field() 覆盖影响）
        if json_fields:
            base = base + ", " + ", ".join(
                self._json_field_expr(col, path, alias)
                for (col, path, alias) in json_fields)
        return base

    def _json_field_expr(self, column: str, path: Any,
                         alias: Optional[str]) -> str:
        """把 JSON 路径字段编译为 ``json_extract(col, path) AS alias``。"""
        expr = self.conn.driver.json_extract(self._qi(column), path)
        return f"{expr} AS {self._qi(alias)}" if alias else expr

    # ------------------------------------------------------------------ #
    # WHERE
    # ------------------------------------------------------------------ #
    def build_where(self, where: List[Tuple[str, Any]]) -> Tuple[str, list]:
        """where 结构: [(logic, cond), ...]，logic 为 'AND'/'OR'/'XOR'。

        cond 支持:
          - ``('field', op, value)`` 三元组
          - ``('field', value)``     二元组（等于简写 / None=IS NULL / 序列=IN）
          - ``dict``                 字段=>值（AND 连接）
          - ``callable``             子查询闭包（内部条件用括号包裹）
          - ``Raw``                  原生 SQL
          - ``list/tuple``           子条件组（AND 连接，各组括号包裹）
        """
        if not where:
            return "", []
        parts: List[str] = []
        params: list = []
        for i, (logic, cond) in enumerate(where):
            sql, p = self._parse_cond(cond)
            if i == 0:
                parts.append(sql)
            else:
                parts.append(f"{logic} {sql}")
            params.extend(p)
        return "WHERE " + " ".join(parts), params

    def _parse_cond(self, cond: Any) -> Tuple[str, list]:
        if isinstance(cond, JsonWhere):
            return self._build_json_condition(cond)
        if isinstance(cond, RawWhere):
            return cond.sql, list(cond.params)
        if is_raw(cond):
            return cond.value, []
        if callable(cond):
            # 闭包子查询：内部条件括号包裹，保证优先级
            from .query import Query  # 延迟导入避免循环
            sub = Query(self.conn)
            cond(sub)
            sub_sql, sub_params = self.build_where(sub.options.get("where", []))
            if not sub_sql:
                return "1 = 1", []
            return f"({sub_sql[6:]})", sub_params  # 去掉 "WHERE " 前缀
        if isinstance(cond, dict):
            parts, params = [], []
            for field, value in cond.items():
                s, p = self._field_condition(field, value)
                parts.append(s)
                params.extend(p)
            if not parts:
                return "1 = 1", []
            return " AND ".join(parts), params
        if isinstance(cond, tuple):
            # (field, value) 或 (field, op, value) 字段条件
            if len(cond) in (2, 3) and isinstance(cond[0], str) and cond[0] != "__group__":
                field, rest = cond[0], cond[1:]
                if len(rest) == 1:
                    return self._field_condition(field, rest[0])
                op, value = rest
                if isinstance(op, str) and op.lower() in _OPS:
                    return self._parse_op(self._qi(field), op, value)
                return self._field_condition(field, rest)
            # 其他 tuple 视为条件组
            return self._cond_group(list(cond))
        if isinstance(cond, list):
            return self._cond_group(cond)
        if isinstance(cond, str):
            # 原生条件字符串（不绑定参数，信任调用方）
            return cond, []
        raise QueryError(f"不支持的 where 条件类型: {type(cond)}")

    def _cond_group(self, conds: List[Any]) -> Tuple[str, list]:
        parts, params = [], []
        for item in conds:
            s, p = self._parse_cond(item)
            parts.append(f"({s})")
            params.extend(p)
        if not parts:
            return "1 = 1", []
        return " AND ".join(parts), params

    def _field_condition(self, field: Any, value: Any) -> Tuple[str, list]:
        field_sql = self._qi(field) if isinstance(field, str) else str(field)
        # 1) 原生表达式值
        if is_raw(value):
            return f"{field_sql} = {value.value}", []
        # 2) (op, value) 元组
        if isinstance(value, (list, tuple)) and len(value) == 2 \
                and isinstance(value[0], str) and value[0].lower() in _OPS:
            return self._parse_op(field_sql, value[0], value[1])
        # 3) 序列 -> IN
        if isinstance(value, (list, tuple, set)):
            if not value:
                return "1 = 0", []
            marks = self.conn.driver.placeholder(len(value))
            return f"{field_sql} IN ({marks})", list(value)
        # 4) None -> IS NULL
        if value is None:
            return f"{field_sql} IS NULL", []
        # 5) 等于
        return f"{field_sql} = ?", [value]

    def _parse_op(self, field_sql: str, op: str, value: Any) -> Tuple[str, list]:
        op = op.lower()
        # 比较运算符 + 原生表达式值：直接拼接不绑定
        if is_raw(value) and op in ("=", "!=", "<>", ">", "<", ">=", "<="):
            sql_op = "<>" if op == "<>" else op.upper()
            return f"{field_sql} {sql_op} {value.value}", []
        if op in ("=", "!=", "<>", ">", "<", ">=", "<="):
            sql_op = "<>" if op == "<>" else op.upper()
            return f"{field_sql} {sql_op} ?", [value]
        if op == "like":
            return f"{field_sql} LIKE ?", [value]
        if op == "not like":
            return f"{field_sql} NOT LIKE ?", [value]
        if op == "in":
            if not value:
                return "1 = 0", []
            marks = self.conn.driver.placeholder(len(value))
            return f"{field_sql} IN ({marks})", list(value)
        if op == "not in":
            if not value:
                return "1 = 1", []
            marks = self.conn.driver.placeholder(len(value))
            return f"{field_sql} NOT IN ({marks})", list(value)
        if op == "between":
            return f"{field_sql} BETWEEN ? AND ?", list(value)
        if op == "not between":
            return f"{field_sql} NOT BETWEEN ? AND ?", list(value)
        if op == "is":
            if value is None:
                return f"{field_sql} IS NULL", []
            return f"{field_sql} IS ?", [value]
        if op == "is not":
            if value is None:
                return f"{field_sql} IS NOT NULL", []
            return f"{field_sql} IS NOT ?", [value]
        if op in ("exp", "raw"):
            expr = value.value if is_raw(value) else value
            return f"{field_sql} {expr}", []
        if op == "find_in_set":
            return f"FIND_IN_SET(?, {field_sql})", [value]
        if op == "exists":
            return self._exists_sql("EXISTS", value)
        if op == "not exists":
            return self._exists_sql("NOT EXISTS", value)
        raise QueryError(f"不支持的比较运算符: {op}")

    # ------------------------------------------------------------------ #
    # JSON 路径条件
    # ------------------------------------------------------------------ #
    def _build_json_condition(self, jw: JsonWhere) -> Tuple[str, list]:
        """把 :class:`JsonWhere` 编译为方言级 SQL 与绑定参数。

        语义约定（完整说明见 ``docs/json-query.md``）：

        - ``is null`` / ``is not null``：路径**值**为 JSON null 或路径不存在
        - ``exists`` / ``not exists``：路径**是否存在**（值为 null 也算存在）
        - ``contains`` / ``not contains``：JSON 数组是否含某标量元素
        - 其余运算符（``=`` / ``>`` / ``like`` / ``in`` / ``between`` …）
          作用于路径提取出的值

        SQL 形态由驱动提供（``json_extract`` / ``json_contains`` …），
        本方法只负责运算符分派与参数绑定顺序。
        """
        driver = self.conn.driver
        if not driver.supports_json:
            raise UnsupportedOperation(
                f"{driver.name} 驱动不支持 JSON 路径查询")

        col = self._qi(jw.column)
        path = jw.path
        op = str(jw.op or "=").strip().lower()
        if op not in JSON_OPS:
            raise QueryError(
                f"不支持的 JSON 条件运算符: {jw.op!r}。可选: "
                + ", ".join(sorted(JSON_OPS)))

        value = jw.value

        # 1) 路径存在性
        if op in ("exists", "not exists"):
            expr = driver.json_exists(col, path)
            return (expr, []) if op == "exists" else (f"NOT {expr}", [])

        # 2) 值为 null / 非 null
        if op in ("is null", "null"):
            return f"{driver.json_extract(col, path)} IS NULL", []
        if op in ("is not null", "not null"):
            return f"{driver.json_extract(col, path)} IS NOT NULL", []

        # 3) 数组元素包含
        if op in ("contains", "not contains"):
            expr = driver.json_contains(col, path, driver.placeholder(1))
            if op == "contains":
                return expr, [self._json_scalar(value)]
            return f"NOT {expr}", [self._json_scalar(value)]

        lhs = self._json_lhs(driver, col, path, jw.func)

        # 4) 集合运算
        if op in ("in", "not in"):
            if not isinstance(value, (list, tuple, set)):
                raise QueryError("JSON in 条件需要序列值（list/tuple/set）")
            values = list(value)
            if not values:
                return ("1 = 0" if op == "in" else "1 = 1"), []
            marks = driver.placeholder(len(values))
            return (f"{lhs} IN ({marks})",
                    [self._json_scalar(v) for v in values])

        if op in ("between", "not between"):
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise QueryError("JSON between 条件需要 [下限, 上限] 两个值")
            keyword = "BETWEEN" if op == "between" else "NOT BETWEEN"
            return (f"{lhs} {keyword} ? AND ?",
                    [self._json_scalar(value[0]), self._json_scalar(value[1])])

        # 5) 模糊匹配与标量比较
        if op in ("like", "not like"):
            keyword = "LIKE" if op == "like" else "NOT LIKE"
            return f"{lhs} {keyword} ?", [value]

        sql_op = "<>" if op == "<>" else op.upper()
        return f"{lhs} {sql_op} ?", [self._json_scalar(value)]

    @staticmethod
    def _json_lhs(driver: Any, column_sql: str, path: Any,
                  func: Optional[str]) -> str:
        """按 ``func`` 取 JSON 条件的左值表达式。

        - ``None`` ：路径值（``json_extract``）
        - ``length``：元素个数（``json_length``）
        - ``type``  ：类型名（``json_type``）
        """
        if func == "length":
            return driver.json_length(column_sql, path)
        if func == "type":
            return driver.json_type(column_sql, path)
        return driver.json_extract(column_sql, path)

    @staticmethod
    def _json_scalar(value: Any) -> Any:
        """转换 JSON 条件的绑定值。

        SQLite / MySQL 的 JSON 布尔以整数 0/1 存储，故 ``True``/``False``
        转为 1/0，否则与 ``json_extract`` 的结果无法比较。

        ``dict`` / ``list`` 不支持直接比较：JSON 文本比较受键顺序与空白
        格式影响，结果不可靠，因此显式报错并引导到正确 API。
        """
        if isinstance(value, bool):
            return 1 if value else 0
        if isinstance(value, (dict, list)):
            raise QueryError(
                "JSON 条件不支持 dict/list 直接比较（JSON 文本比较受键序与"
                "格式影响，结果不可靠）：数组元素匹配请用 where_json_contains()，"
                "复杂结构匹配请用 where_raw() 配合 json_extract()")
        return value

    def _exists_sql(self, keyword: str, value: Any) -> Tuple[str, list]:
        if is_raw(value):
            return f"{keyword} ({value.value})", []
        if callable(value):
            from .query import Query
            sub = Query(self.conn)
            value(sub)
            sql, params = self._subquery_sql(sub)
            return f"{keyword} ({sql})", params
        if isinstance(value, (list, tuple)) and len(value) == 2:
            # (sql, params) 原生子查询
            return f"{keyword} ({value[0]})", list(value[1] or [])
        raise QueryError("exists 条件需要闭包或原生 SQL")

    def _subquery_sql(self, sub: Any) -> Tuple[str, list]:
        opts = sub.options
        if not opts.get("table"):
            raise QueryError("子查询未指定数据表")
        sql, params = self.select(opts)
        return sql, params

    # ------------------------------------------------------------------ #
    # JOIN / GROUP / HAVING / UNION / ORDER / LIMIT
    # ------------------------------------------------------------------ #
    def _build_join(self, options: dict) -> Tuple[str, list]:
        joins = options.get("join", [])
        if not joins:
            return "", []
        parts, params = [], []
        for j in joins:
            jtype = j.get("type", "INNER").upper()
            if jtype not in _JOIN_TYPES:
                raise QueryError(f"不支持的 JOIN 类型: {jtype}")
            table = j["table"]
            t = j.get("alias")
            name = f"{self._qi(table)} {self._qi(t)}" if t else self._qi(table)
            on = j.get("on")
            if callable(on):
                # 闭包: on(lambda q: q.where_column('a.user_id', '=', 'u.id'))
                from .query import Query
                sub = Query(self.conn)
                on(sub)
                cond_sql, p = self.build_where(sub.options.get("where", []))
                cond_sql = cond_sql[6:] if cond_sql.startswith("WHERE ") else cond_sql
                params.extend(p)
            elif isinstance(on, RawWhere):
                cond_sql, p = on.sql, list(on.params)
                params.extend(p)
            elif is_raw(on):
                cond_sql = on.value
            else:
                cond_sql, p = self.build_where(on)
                cond_sql = cond_sql[6:] if cond_sql.startswith("WHERE ") else cond_sql
                params.extend(p)
            parts.append(f"{jtype} JOIN {name} ON {cond_sql}")
        return " ".join(parts), params

    def _build_group(self, options: dict) -> str:
        group = options.get("group")
        if not group:
            return ""
        if isinstance(group, Raw):
            return f"GROUP BY {group.value}"
        fields = group if isinstance(group, (list, tuple)) else [g.strip() for g in str(group).split(",") if g.strip()]
        return "GROUP BY " + ", ".join(self._qi(f) for f in fields)

    def _build_having(self, options: dict) -> Tuple[str, list]:
        having = options.get("having")
        if not having:
            return "", []
        sql, params = self.build_where(having)
        if sql:
            sql = "HAVING" + sql[len("WHERE"):]
        return sql, params

    def _build_union(self, options: dict) -> Tuple[str, list]:
        unions = options.get("union", [])
        if not unions:
            return "", []
        parts, params = [], []
        for u in unions:
            kind = "UNION ALL" if u.get("all") else "UNION"
            target = u.get("query")
            if is_raw(target):
                parts.append(f"{kind} {target.value}")
            elif callable(target):
                from .query import Query
                sub = Query(self.conn)
                target(sub)
                sql, p = self.select(sub.options)
                parts.append(f"{kind} {sql}")
                params.extend(p)
            else:
                raise QueryError("union 需要闭包或原生 SQL")
        return " ".join(parts), params

    def _build_order(self, options: dict) -> str:
        order = options.get("order")
        if not order:
            return ""
        parts = parse_order(order, self._qi)
        if parts:
            return "ORDER BY " + ", ".join(parts)
        return ""

    def _build_limit(self, options: dict) -> str:
        limit = options.get("limit")
        if limit is None:
            return ""
        # 原生片段完全由调用方负责（驱动不解释 Raw）
        if isinstance(limit, Raw):
            return f"LIMIT {limit.value}"
        driver = self.conn.driver
        if isinstance(limit, (list, tuple)):
            if len(limit) == 1:
                return driver.limit_sql(limit[0])
            return driver.limit_sql(limit[0], limit[1])
        return driver.limit_sql(limit)

    # ------------------------------------------------------------------ #
    # JSON 路径写入（局部更新）
    # ------------------------------------------------------------------ #
    def _build_json_update(self, specs: List[JsonUpdate]) -> Tuple[list, list]:
        """把 JSON 路径写入规格编译为 SET 子句与绑定参数。

        编译策略（顺序敏感）：

        1. **按列分组**：同一列的多个规格必须合并进一次调用——SQL 中同一
           列出现多个 SET 子句时只有最后一个生效，其余静默丢失；
        2. **相邻同类合并**：连续的 ``set`` / ``insert`` 合成一次调用
           （``json_set(col, p1, v1, p2, v2)``），``remove`` 的多路径同理；
        3. **异类按序嵌套**：``set`` 与 ``remove`` 混用时折叠为
           ``json_remove(json_set(col, ...), ...)``。折叠是左嵌套，故
           占位符在 SQL 中的出现顺序与参数追加顺序严格一致。

        返回 ``(SET 子句列表, 绑定参数列表)``。
        """
        driver = self.conn.driver
        if not getattr(driver, "supports_json", False):
            raise UnsupportedOperation(
                f"{driver.name} 驱动不支持 JSON 路径写入")
        ph = driver.placeholder(1)

        grouped: "OrderedDict[str, List[JsonUpdate]]" = OrderedDict()
        for spec in specs:
            if spec.mode not in JSON_UPDATE_MODES:
                raise QueryError(
                    f"不支持的 JSON 写入模式: {spec.mode!r}。可选: "
                    + ", ".join(JSON_UPDATE_MODES))
            grouped.setdefault(spec.column, []).append(spec)

        set_sql: list = []
        params: list = []
        for column, ops in grouped.items():
            # ifnull 只能作用于最内层基表达式（外层已保证非 NULL）
            ifnull = next((o.ifnull for o in ops if o.ifnull is not None), None)
            expr = self._qi(column)
            first = True
            i, total = 0, len(ops)
            while i < total:
                mode = ops[i].mode
                j = i
                while j < total and ops[j].mode == mode:
                    j += 1
                chunk = ops[i:j]
                base_null = ifnull if first else None

                if mode == "remove":
                    expr = driver.json_remove(
                        expr, [json_path_literal(o.path) for o in chunk],
                        base_null)
                elif mode == "patch":
                    for k, o in enumerate(chunk):
                        frag, needs, bind_val = self._json_value_fragment(
                            driver, ph, o.value)
                        expr = driver.json_patch(
                            expr, frag, base_null if k == 0 else None)
                        if needs:
                            params.append(bind_val)
                else:
                    pairs, chunk_params = [], []
                    for o in chunk:
                        frag, needs, bind_val = self._json_value_fragment(
                            driver, ph, o.value)
                        pairs.append((json_path_literal(o.path), frag))
                        if needs:
                            chunk_params.append(bind_val)
                    fn = driver.json_set if mode == "set" else driver.json_insert
                    expr = fn(expr, pairs, base_null)
                    params.extend(chunk_params)

                first = False
                i = j
            set_sql.append(f"{self._qi(column)} = {expr}")
        return set_sql, params

    @classmethod
    def _json_value_fragment(cls, driver: Any, placeholder: str,
                             value: Any) -> Tuple[str, bool, Any]:
        """把写入值编译为 SQL 片段与配套绑定值。

        返回 ``(片段, 是否需要绑定, 绑定值)``：

        - ``Raw``：片段直接内联（高级用法，如写入
          ``json_extract(other, '$.x')`` 的结果），不产生绑定；
        - 容器 / 布尔：片段由 :meth:`Driver.json_bind` 包装（SQLite 为
          ``json(?)``），绑定值取 **JSON 文本**；
        - 其余标量：片段为裸占位符，绑定值原样。
        """
        if is_raw(value):
            return value.value, False, None
        return (driver.json_bind(placeholder, value), True,
                cls._json_bind_value(driver, value))

    @staticmethod
    def _json_bind_value(driver: Any, value: Any) -> Any:
        """求 JSON 写入的绑定值。

        - 容器：交驱动编码（SQL 驱动得到 JSON 文本，NoSQL 保持原生对象）；
        - ``bool``：JSON 文本 ``'true'`` / ``'false'``——直接绑定会被
          sqlite3 适配成整数 1/0，落库后 ``json_type`` 报 ``integer``，
          下游看到数字而非 JSON 布尔，丢失类型保真；
        - 其余（数字 / 文本 / ``None``）：原样绑定。
        """
        if isinstance(value, bool):
            return "true" if value else "false"
        return driver.json_encode(value)

    # ------------------------------------------------------------------ #
    # INSERT / UPDATE / DELETE
    # ------------------------------------------------------------------ #
    def insert(self, options: dict, data: dict) -> Tuple[str, list]:
        table = self._build_table(options)
        fields = list(data.keys())
        marks = self.conn.driver.placeholder(len(fields))
        cols = ", ".join(self.conn.driver.quote_identifier(f) for f in fields)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({marks})"
        # dict / list 值自动编码为 JSON 文本（驱动层 json_encode 负责方言差异）
        return sql, [self.conn.driver.json_encode(data[f]) for f in fields]

    def insert_all(self, options: dict, data_list: List[dict]) -> Tuple[str, list]:
        table = self._build_table(options)
        if not data_list:
            raise QueryError("insert_all 数据不能为空")
        fields = list(data_list[0].keys())
        # 校验每行字段一致（think-orm 要求一致）
        for row in data_list:
            if set(row.keys()) != set(fields):
                raise QueryError("insert_all 各行字段必须一致")
        cols = ", ".join(self._qi(f) for f in fields)
        rows_sql, params = [], []
        for row in data_list:
            rows_sql.append("(" + self.conn.driver.placeholder(len(fields)) + ")")
            params.extend(self.conn.driver.json_encode(row[f]) for f in fields)
        sql = f"INSERT INTO {table} ({cols}) VALUES {', '.join(rows_sql)}"
        return sql, params

    def update(self, options: dict, data: dict) -> Tuple[str, list]:
        """编译 UPDATE 语句。

        ``data`` 为普通字段赋值（``dict``/``list`` 值自动 JSON 编码），
        ``options['json_update']`` 为 JSON 路径写入规格。二者作用于同一
        字段时必须报错——SQL 中同一列出现多个 SET 子句只有最后一个生效，
        会静默丢掉其中一个赋值。
        """
        table = self._build_table(options)
        specs: List[JsonUpdate] = options.get("json_update") or []
        if specs and data:
            overlap = set(data) & {s.column for s in specs}
            if overlap:
                raise QueryError(
                    "字段不能同时出现在普通更新数据与 JSON 路径更新中："
                    + ", ".join(sorted(overlap))
                    + "（同列多个 SET 子句只有最后一个生效，会静默丢更新）")
        set_sql, set_params = [], []
        for field, value in data.items():
            if is_raw(value):
                set_sql.append(f"{self._qi(field)} = {value.value}")
            else:
                set_sql.append(f"{self._qi(field)} = ?")
                set_params.append(self.conn.driver.json_encode(value))
        if specs:
            json_sql, json_params = self._build_json_update(specs)
            set_sql.extend(json_sql)
            set_params.extend(json_params)
        if not set_sql:
            raise QueryError("update 必须指定要更新的字段")
        where_sql, where_params = self.build_where(options.get("where", []))
        if not where_sql:
            raise QueryError("update 必须指定 where 条件（防止全表更新）")
        sql = f"UPDATE {table} SET {', '.join(set_sql)} {where_sql}"
        return sql, set_params + where_params

    def delete(self, options: dict) -> Tuple[str, list]:
        table = self._build_table(options)
        where_sql, where_params = self.build_where(options.get("where", []))
        sql = f"DELETE FROM {table}"
        if where_sql:
            sql += f" {where_sql}"
        return sql, where_params
