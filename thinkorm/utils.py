"""工具函数与原生表达式支持（对应 think-orm 的 Raw 表达式、标识符引用等）。"""
from __future__ import annotations

import re
from typing import Any, List, Union

__all__ = [
    "Raw", "raw", "quote_ident", "to_snake", "to_camel",
    "parse_field", "parse_order", "is_raw",
]

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Raw:
    """原生 SQL 表达式包装。

    用于 where 值 / 字段 / 数据值中插入原始 SQL 片段（不做参数绑定），
    对应 think-orm 的 ``Db::raw()``。用法::

        query.update({'balance': raw('`balance` - 100')})
        query.where('score', '>', raw('AVG(score)'))
    """

    __slots__ = ("value",)

    def __init__(self, value: str):
        if not isinstance(value, str):
            raise TypeError("Raw expression must be a string")
        self.value = value

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Raw({self.value!r})"

    def __str__(self) -> str:
        return self.value


def raw(expr: str) -> Raw:
    """构造原生表达式（``Db.raw`` 的等价物）。"""
    return Raw(expr)


def is_raw(value: Any) -> bool:
    return isinstance(value, Raw)


def quote_ident(ident: Union[str, Raw]) -> str:
    """引用标识符（表名/字段名），防止标识符注入。

    - ``user`` -> ```user```
    - ``u.id``  -> ```u```.```id```
    - ``*``     -> ``*``
    - 复杂表达式（``COUNT(*)``、``name AS n``）按表达式原样返回，
      调用方应确保来源可信（开发者代码而非用户输入）。
    """
    if isinstance(ident, Raw):
        return ident.value
    ident = str(ident).strip()
    if ident == "*":
        return "*"
    if _IDENT_RE.match(ident):
        return f"`{ident}`"
    if "." in ident:
        return ".".join(quote_ident(p) for p in ident.split("."))
    return ident


def quote_fields(fields: Union[str, List[str], Raw]) -> str:
    """批量引用字段，支持 ``"id,name"`` / ``["id","name"]`` / Raw。"""
    if isinstance(fields, Raw):
        return fields.value
    if isinstance(fields, str):
        return ", ".join(quote_ident(f) for f in fields.split(",") if f.strip())
    return ", ".join(quote_ident(f) for f in fields)


def to_snake(name: str) -> str:
    """camelCase -> snake_case（``whereOr`` -> ``where_or``）。"""
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return name.replace("__", "_")


def to_camel(name: str) -> str:
    """snake_case -> camelCase（``where_or`` -> ``whereOr``）。"""
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def parse_field(field: Union[str, list, dict, Raw]) -> List[str]:
    """把字段定义解析为 SQL 片段列表。

    支持::

        parse_field('id,name')            # ['`id`', '`name`']
        parse_field(['id', 'name'])       # ['`id`', '`name`']
        parse_field({'uid': 'id', 'uname': 'name'})  # ['`id` AS `uid`', ...]
    """
    if isinstance(field, Raw):
        return [field.value]
    if isinstance(field, str):
        return [quote_ident(f) for f in field.split(",") if f.strip()]
    if isinstance(field, (list, tuple)):
        return [quote_ident(f) for f in field]
    if isinstance(field, dict):
        # {别名: 字段}，与 think-orm 的 field(['user_id' => 'id']) 语义一致
        return [f"{quote_ident(col)} AS {quote_ident(alias)}" for alias, col in field.items()]
    raise TypeError(f"Unsupported field type: {type(field)}")


def parse_order(order: Union[str, list, dict, Raw]) -> List[str]:
    """把排序定义解析为 SQL 片段列表。

    支持::

        parse_order('id desc')                 # ['`id` DESC']
        parse_order('id desc, name asc')
        parse_order(['id', 'name'])            # 默认 ASC
        parse_order({'id': 'desc', 'name': 'asc'})
    """
    if isinstance(order, Raw):
        return [order.value]
    if isinstance(order, str):
        parts = []
        for item in order.split(","):
            item = item.strip()
            if not item:
                continue
            m = re.match(r"^(.+?)(?:\s+(ASC|DESC))?$", item, re.IGNORECASE)
            field, direction = m.groups() if m else (item, None)
            parts.append(f"{quote_ident(field.strip())} {(direction or 'ASC').upper()}")
        return parts
    if isinstance(order, (list, tuple)):
        return [f"{quote_ident(f)} ASC" for f in order]
    if isinstance(order, dict):
        return [f"{quote_ident(k)} {(v or 'ASC').upper()}" for k, v in order.items()]
    raise TypeError(f"Unsupported order type: {type(order)}")
