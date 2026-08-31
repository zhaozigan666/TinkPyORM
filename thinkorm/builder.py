"""SQL 构造器（对应 think-orm 的 BaseBuilder/Builder）。

将 Query 的 options 编译为 SQL 与绑定参数，所有用户值一律走 ``?``
参数绑定，标识符经 quote_ident 引用，从机制上杜绝 SQL 注入。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .connection import Connection
from .exceptions import QueryError
from .utils import Raw, quote_ident, parse_field, parse_order, is_raw

__all__ = ["Builder", "RawWhere"]

# 支持的比较运算符
_OPS = {
    "=", "!=", "<>", ">", "<", ">=", "<=",
    "like", "not like", "in", "not in", "between", "not between",
    "is", "is not", "exists", "not exists", "find_in_set", "exp", "raw",
}

_JOIN_TYPES = {"INNER", "LEFT", "RIGHT", "FULL", "CROSS"}


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
                name = ".".join(quote_ident(x) for x in name.split("."))
            else:
                name = quote_ident(name)
            parts.append(f"{name} {quote_ident(alias)}" if alias else name)
        return ", ".join(parts)

    def _build_field(self, options: dict) -> str:
        fields = options.get("field")
        without = options.get("without_field")
        if fields:
            return ", ".join(parse_field(fields))
        if without:
            # SQLite 不支持 SELECT * EXCEPT：基于表结构生成排除后的字段列表
            tables = options.get("table") or []
            if len(tables) != 1:
                raise QueryError("without_field 仅支持单表查询")
            table = tables[0]["name"]
            if table.startswith("("):  # 子查询场景不处理
                raise QueryError("without_field 不支持子查询表")
            try:
                all_fields = [r["name"] for r in self.conn.query(f"PRAGMA table_info({quote_ident(table)})")]
            except Exception:
                raise QueryError("without_field 需要表已存在以读取表结构")
            exclude = set(without) if isinstance(without, (list, tuple, set)) else {f.strip() for f in str(without).split(",") if f.strip()}
            keep = [f for f in all_fields if f not in exclude]
            if not keep:
                raise QueryError("without_field 排除后无可用字段")
            return ", ".join(quote_ident(f) for f in keep)
        return "*"

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
                    return self._parse_op(quote_ident(field), op, value)
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
        field_sql = quote_ident(field) if isinstance(field, str) else str(field)
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
            marks = ", ".join("?" * len(value))
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
            marks = ", ".join("?" * len(value))
            return f"{field_sql} IN ({marks})", list(value)
        if op == "not in":
            if not value:
                return "1 = 1", []
            marks = ", ".join("?" * len(value))
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
            name = f"{quote_ident(table)} {quote_ident(t)}" if t else quote_ident(table)
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
        return "GROUP BY " + ", ".join(quote_ident(f) for f in fields)

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
        parts = parse_order(order)
        if parts:
            return "ORDER BY " + ", ".join(parts)
        return ""

    def _build_limit(self, options: dict) -> str:
        limit = options.get("limit")
        if limit is None:
            return ""
        if isinstance(limit, Raw):
            return f"LIMIT {limit.value}"
        if isinstance(limit, (list, tuple)):
            if len(limit) == 1:
                return f"LIMIT {int(limit[0])}"
            return f"LIMIT {int(limit[0])} OFFSET {int(limit[1])}"
        return f"LIMIT {int(limit)}"

    # ------------------------------------------------------------------ #
    # INSERT / UPDATE / DELETE
    # ------------------------------------------------------------------ #
    def insert(self, options: dict, data: dict) -> Tuple[str, list]:
        table = self._build_table(options)
        fields = list(data.keys())
        marks = ", ".join("?" * len(fields))
        cols = ", ".join(quote_ident(f) for f in fields)
        sql = f"INSERT INTO {table} ({cols}) VALUES ({marks})"
        return sql, [data[f] for f in fields]

    def insert_all(self, options: dict, data_list: List[dict]) -> Tuple[str, list]:
        table = self._build_table(options)
        if not data_list:
            raise QueryError("insert_all 数据不能为空")
        fields = list(data_list[0].keys())
        # 校验每行字段一致（think-orm 要求一致）
        for row in data_list:
            if set(row.keys()) != set(fields):
                raise QueryError("insert_all 各行字段必须一致")
        cols = ", ".join(quote_ident(f) for f in fields)
        rows_sql, params = [], []
        for row in data_list:
            rows_sql.append("(" + ", ".join("?" * len(fields)) + ")")
            params.extend(row[f] for f in fields)
        sql = f"INSERT INTO {table} ({cols}) VALUES {', '.join(rows_sql)}"
        return sql, params

    def update(self, options: dict, data: dict) -> Tuple[str, list]:
        table = self._build_table(options)
        set_sql, set_params = [], []
        for field, value in data.items():
            if is_raw(value):
                set_sql.append(f"{quote_ident(field)} = {value.value}")
            else:
                set_sql.append(f"{quote_ident(field)} = ?")
                set_params.append(value)
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
