"""工具函数与原生表达式支持（对应 think-orm 的 Raw 表达式、字段解析等）。

v0.3.0 起标识符引用统一走 :meth:`tinkpyorm.drivers.Driver.quote_identifier`
以便支持多种数据库方言；本模块仅保留 ``Raw`` / ``raw`` / ``parse_*`` 等
无方言依赖的工具函数。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Union

__all__ = [
    "Raw", "raw", "to_snake", "to_camel",
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


def to_snake(name: str) -> str:
    """camelCase -> snake_case（``whereOr`` -> ``where_or``）。"""
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    return name.replace("__", "_")


def to_camel(name: str) -> str:
    """snake_case -> camelCase（``where_or`` -> ``whereOr``）。"""
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def parse_field(field: Union[str, list, dict, Raw],
                quote: Callable[[str], str]) -> list:
    """把字段定义解析为 SQL 片段列表。

    ``quote`` 是方言级标识符引用函数（通常是 ``driver.quote_identifier``），
    由调用方按当前连接的数据库方言传入。

    支持::

        parse_field('id,name', quote)            # ["`id`", "`name`"]
        parse_field(['id', 'name'], quote)       # ["`id`", "`name`"]
        parse_field({'uid': 'id'}, quote)        # ["`id` AS `uid`"]
    """
    if isinstance(field, Raw):
        return [field.value]
    if isinstance(field, str):
        return [quote(f) for f in field.split(",") if f.strip()]
    if isinstance(field, (list, tuple)):
        return [quote(f) for f in field]
    if isinstance(field, dict):
        return [f"{quote(col)} AS {quote(alias)}" for alias, col in field.items()]
    raise TypeError(f"Unsupported field type: {type(field)}")


def parse_order(order: Union[str, list, dict, Raw],
                quote: Callable[[str], str]) -> list:
    """把排序定义解析为 SQL 片段列表（``quote`` 同 :func:`parse_field`）。

    支持::

        parse_order('id desc', quote)                 # ["`id` DESC"]
        parse_order(['id', 'name'], quote)            # ["`id` ASC", "`name` ASC"]
        parse_order({'id': 'desc'}, quote)            # ["`id` DESC"]
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
            parts.append(f"{quote(field.strip())} {(direction or 'ASC').upper()}")
        return parts
    if isinstance(order, (list, tuple)):
        return [f"{quote(f)} ASC" for f in order]
    if isinstance(order, dict):
        return [f"{quote(k)} {(v or 'ASC').upper()}" for k, v in order.items()]
    raise TypeError(f"Unsupported order type: {type(order)}")
