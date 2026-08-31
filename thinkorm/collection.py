"""结果集封装（对应 think-orm 的 Collection / Paginator）。"""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional


class Collection(list):
    """查询结果集：list 子类，提供 think-orm Collection 常用方法。

    元素可为 dict 或模型实例。绑定模型时 ``model`` 为对应模型类。
    """

    def __init__(self, items: Optional[list] = None, model: Optional[type] = None):
        super().__init__(items or [])
        self.model = model

    # -- 取数 -- #
    def first(self) -> Any:
        return self[0] if self else None

    def last(self) -> Any:
        return self[-1] if self else None

    def column(self, field: str, key: Optional[str] = None) -> list:
        """取字段值列表；``column('name', 'id')`` 以 key 字段为键。"""
        values = []
        for item in self:
            if isinstance(item, dict):
                values.append(item.get(field))
            else:
                values.append(getattr(item, field, None))
        if key is None:
            return values
        result = {}
        for item, v in zip(self, values):
            k = item.get(key) if isinstance(item, dict) else getattr(item, key, None)
            result[k] = v
        return result

    def value(self, field: str, default: Any = None) -> Any:
        row = self.first()
        if row is None:
            return default
        if isinstance(row, dict):
            return row.get(field, default)
        return getattr(row, field, default)

    # -- 过滤 / 变换 -- #
    def where(self, **kwargs: Any) -> "Collection":
        """按字段过滤（AND 语义），返回新集合。"""
        def match(item: Any) -> bool:
            for k, v in kwargs.items():
                got = item.get(k) if isinstance(item, dict) else getattr(item, k, None)
                if got != v:
                    return False
            return True
        return Collection([i for i in self if match(i)], model=self.model)

    def filter_(self, callback: Callable[[Any], bool]) -> "Collection":
        return Collection([i for i in self if callback(i)], model=self.model)

    def each(self, callback: Callable[[Any], Any]) -> "Collection":
        for i, item in enumerate(self):
            self[i] = callback(item)
        return self

    def map(self, callback: Callable[[Any], Any]) -> "Collection":
        return Collection([callback(i) for i in self], model=self.model)

    # -- 聚合 -- #
    def sum(self, field: str) -> Any:
        return sum(self.column(field) or [0])

    def avg(self, field: str) -> float:
        vals = [v for v in self.column(field) if v is not None]
        return sum(vals) / len(vals) if vals else 0.0

    def max(self, field: str) -> Any:
        vals = [v for v in self.column(field) if v is not None]
        return max(vals) if vals else None

    def min(self, field: str) -> Any:
        vals = [v for v in self.column(field) if v is not None]
        return min(vals) if vals else None

    # -- 输出 -- #
    def to_array(self) -> List[dict]:
        """转为纯 dict 列表（模型实例调用 to_dict）。"""
        out = []
        for item in self:
            if isinstance(item, dict):
                out.append(dict(item))
            elif hasattr(item, "to_dict"):
                out.append(item.to_dict())
            else:
                out.append(dict(item))
        return out

    def to_json(self, **kwargs: Any) -> str:
        import json
        # default=str：让 date/datetime/time 等类型转换字段可序列化为 ISO 字符串
        kwargs.setdefault("default", str)
        return json.dumps(self.to_array(), ensure_ascii=False, **kwargs)

    def hidden(self, *fields: str) -> "Collection":
        for item in self:
            if hasattr(item, "hidden"):
                item.hidden(*fields)
        return self

    def visible(self, *fields: str) -> "Collection":
        for item in self:
            if hasattr(item, "visible"):
                item.visible(*fields)
        return self

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Collection({list.__repr__(self)})"


class Paginator:
    """分页结果对象（对应 think-orm Paginator）。"""

    def __init__(self, items: Any, total: int, current_page: int, list_rows: int):
        self.items = items          # 当前页数据（Collection）
        self.total = total          # 总记录数
        self.current_page = current_page  # 当前页码
        self.list_rows = list_rows  # 每页条数

    @property
    def last_page(self) -> int:
        """总页数。"""
        if self.total == 0:
            return 0
        return (self.total + self.list_rows - 1) // self.list_rows

    @property
    def has_more(self) -> bool:
        return self.current_page < self.last_page

    @property
    def per_page(self) -> int:
        return self.list_rows

    @property
    def data(self) -> Any:
        return self.items

    def to_array(self) -> dict:
        return {
            "total": self.total,
            "per_page": self.list_rows,
            "current_page": self.current_page,
            "last_page": self.last_page,
            "data": self.items.to_array() if hasattr(self.items, "to_array") else list(self.items),
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"Paginator(total={self.total}, page={self.current_page}/{self.last_page}, rows={self.list_rows})"
