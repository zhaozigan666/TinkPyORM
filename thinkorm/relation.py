"""模型关联（对应 think-orm 的 Relation 家族）。

支持四种核心关联 + 预载入（eager loading，对应 with()）与懒加载：

- has_one          一对一（当前模型拥有一个关联）
- has_many         一对多（当前模型拥有多个关联）
- belongs_to       一对一反向（当前模型属于某个父模型）
- belongs_to_many  多对多（经中间表）
"""
from __future__ import annotations

from typing import Any, Callable, List, Optional

from .exceptions import RelationNotFound
from .utils import to_snake

# 关联类型常量
HAS_ONE = "has_one"
HAS_MANY = "has_many"
BELONGS_TO = "belongs_to"
BELONGS_TO_MANY = "belongs_to_many"


class Relation:
    """关联基类：持有关联元信息，提供批量预载入与单条懒加载。"""

    def __init__(
        self,
        parent: Any,
        target: Any,
        type_: str,
        foreign_key: Optional[str] = None,
        local_key: Optional[str] = None,
        middle: Optional[str] = None,
    ):
        self.parent = parent          # 父模型实例（定义关联的模型）
        self.name: Optional[str] = None  # 关联名（方法名），由框架注入
        self.type = type_
        self.target = self._resolve_target(target)
        self.foreign_key = foreign_key
        self.local_key = local_key
        self.middle = middle
        self._resolve_keys()

    # ------------------------------------------------------------------ #
    @staticmethod
    def _resolve_target(target: Any) -> type:
        if isinstance(target, type):
            return target
        if isinstance(target, str):
            import sys
            for mod in list(sys.modules.values()):
                t = getattr(mod, target, None)
                if isinstance(t, type):
                    return t
            raise RelationNotFound(f"无法解析目标模型类: {target!r}")
        raise TypeError(f"target 必须是模型类或类名字符串，收到 {type(target)}")

    def _resolve_keys(self) -> None:
        parent_cls = type(self.parent)
        self.parent_table = parent_cls._table_name()
        self.parent_pk = parent_cls.__pk__
        self.target_table = self.target._table_name()
        self.target_pk = self.target.__pk__

        if self.type in (HAS_ONE, HAS_MANY):
            # 关联表（目标表）持有外键，默认 父表名_父主键（如 user_id）
            if not self.foreign_key:
                self.foreign_key = f"{self.parent_table}_{self.parent_pk}"
            if not self.local_key:
                self.local_key = self.parent_pk
        elif self.type == BELONGS_TO:
            # 当前表持有外键，默认 目标表名_目标主键；local_key 为目标表主键
            if not self.foreign_key:
                self.foreign_key = f"{self.target_table}_{self.target_pk}"
            if not self.local_key:
                self.local_key = self.target_pk
        elif self.type == BELONGS_TO_MANY:
            # middle 默认 父表_目标表；foreign_key 为中间表中指向当前模型的列
            if not self.middle:
                names = sorted([self.parent_table, self.target_table])
                self.middle = f"{names[0]}_{names[1]}"
            if not self.foreign_key:
                self.foreign_key = f"{self.parent_table}_{self.parent_pk}"
            if not self.local_key:
                self.local_key = f"{self.target_table}_{self.target_pk}"

    # ------------------------------------------------------------------ #
    # 懒加载（单个父模型）
    # ------------------------------------------------------------------ #
    def get_result(self, parent: Any) -> Any:
        """查询单个父模型的关联数据（懒加载）。"""
        if self.type in (HAS_ONE, HAS_MANY):
            key = parent.get_attr(self.local_key)
            if key is None:
                return None if self.type == HAS_ONE else self._empty_collection()
            q = self.target.query().where(self.foreign_key, key)
            return q.find() if self.type == HAS_ONE else q.select()
        if self.type == BELONGS_TO:
            key = parent.get_attr(self.foreign_key)
            if key is None:
                return None
            return self.target.query().where(self.local_key, key).find()
        if self.type == BELONGS_TO_MANY:
            key = parent.get_attr(self.parent_pk)
            if key is None:
                return self._empty_collection()
            from . import Db
            mids = Db.table(self.middle).where(self.foreign_key, key).column(self.local_key)
            if not mids:
                return self._empty_collection()
            return self.target.query().where_in(self.target_pk, mids).select()
        raise RelationNotFound(f"未知关联类型: {self.type}")

    def _empty_collection(self):
        from .collection import Collection
        return Collection([], model=self.target)

    # ------------------------------------------------------------------ #
    # 预载入（批量）
    # ------------------------------------------------------------------ #
    def eagerly(self, models: list, closure: Optional[Callable] = None) -> None:
        """批量加载关联数据并写入每个模型的 ``_relation[name]``。

        :param closure: 关联查询约束回调 ``closure(query)``
        """
        if not models:
            return
        if self.type in (HAS_ONE, HAS_MANY):
            self._eager_has(models, closure)
        elif self.type == BELONGS_TO:
            self._eager_belongs_to(models, closure)
        elif self.type == BELONGS_TO_MANY:
            self._eager_belongs_to_many(models, closure)

    def _eager_has(self, models: list, closure: Optional[Callable]) -> None:
        keys = []
        for m in models:
            v = m.get_attr(self.local_key)
            if v is not None:
                keys.append(v)
        keys = _unique(keys)
        groups = {}
        if keys:
            q = self.target.query()
            if closure:
                closure(q)
            rows = q.where_in(self.foreign_key, keys).select()
            for row in rows:
                fk = row.get_attr(self.foreign_key)
                groups.setdefault(fk, []).append(row)
        for m in models:
            matched = groups.get(m.get_attr(self.local_key), [])
            if self.type == HAS_ONE:
                m._relation[self.name] = matched[0] if matched else None
            else:
                from .collection import Collection
                m._relation[self.name] = Collection(matched, model=self.target)

    def _eager_belongs_to(self, models: list, closure: Optional[Callable]) -> None:
        keys = []
        for m in models:
            v = m.get_attr(self.foreign_key)
            if v is not None:
                keys.append(v)
        keys = _unique(keys)
        groups = {}
        if keys:
            q = self.target.query()
            if closure:
                closure(q)
            rows = q.where_in(self.local_key, keys).select()
            for row in rows:
                groups[row.get_attr(self.local_key)] = row
        for m in models:
            m._relation[self.name] = groups.get(m.get_attr(self.foreign_key))

    def _eager_belongs_to_many(self, models: list, closure: Optional[Callable]) -> None:
        from . import Db
        from .collection import Collection

        keys = []
        for m in models:
            v = m.get_attr(self.parent_pk)
            if v is not None:
                keys.append(v)
        keys = _unique(keys)
        middle_rows = []
        if keys:
            middle_rows = Db.table(self.middle).where_in(self.foreign_key, keys).select()
        target_keys = []
        for r in middle_rows:
            v = r.get(self.local_key)
            if v is not None:
                target_keys.append(v)
        target_map = {}
        if target_keys:
            q = self.target.query()
            if closure:
                closure(q)
            targets = q.where_in(self.target_pk, _unique(target_keys)).select()
            target_map = {t.get_attr(self.target_pk): t for t in targets}
        for m in models:
            pk = m.get_attr(self.parent_pk)
            related = []
            for r in middle_rows:
                if r.get(self.foreign_key) == pk:
                    t = target_map.get(r.get(self.local_key))
                    if t is not None:
                        related.append(t)
            m._relation[self.name] = Collection(related, model=self.target)


def _unique(items: List[Any]) -> List[Any]:
    return list(dict.fromkeys(items))


def _relation_method(type_: str):
    """生成关联定义方法的工厂（has_one / has_many / ...）。"""

    def define(self, target, foreign_key=None, local_key=None, middle=None):
        return Relation(self, target, type_, foreign_key, local_key, middle)

    define.__name__ = type_
    define.__doc__ = f"定义 {type_} 关联"
    return define
