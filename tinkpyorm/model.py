"""模型基类（对应 think-orm 的 Model，ActiveRecord 模式）。

特性：
- 静态查询入口：User::find(1) / User::where(...).select() / User::create(...)
- 实例持久化：$user->name = 'x'; $user->save() / $user->delete()
- 类型转换（__type__/__json__）、自动时间戳、软删除
- 获取器/修改器（get_xxx_attr / set_xxx_attr）、模型事件（on_before_*）
- 关联定义（has_one / has_many / belongs_to / belongs_to_many）与懒加载/预载入
- 查询范围（scope_xxx，classmethod 定义后可直接链式调用）
"""
from __future__ import annotations

import json
from datetime import date, datetime, time as dtime
from typing import Any, Callable, Dict, List, Optional, Union

from .collection import Collection
from .exceptions import OrmError, QueryError, RelationNotFound
from .relation import Relation, HAS_ONE, HAS_MANY, BELONGS_TO, BELONGS_TO_MANY
from .utils import to_snake

_GETTER_PREFIXES = ("get_", "set_", "on_", "scope_", "search_", "_")


class MetaModel(type):
    """模型元类：

    1. 提供 camelCase 别名（findOrFail -> find_or_fail，与 think-orm 文档一致）
    2. 查询范围 scope_xxx 链式入口
    3. 未定义的静态调用代理到查询构造器（User::where(...) -> User.query().where(...)）
    """

    def __getattr__(cls, name: str) -> Any:
        if name.startswith("_") or name.startswith("scope_"):
            raise AttributeError(f"type object {cls.__name__!r} has no attribute {name!r}")
        snake = to_snake(name)
        # 1) camelCase -> snake_case 同名方法（基于 MRO 直查，避免递归）
        if snake != name:
            for c in cls.__mro__:
                if snake in c.__dict__:
                    return getattr(cls, snake)
        # 2) 查询范围 scope_xxx（支持两种定义风格:
        #    @classmethod def scope_active(cls, query)   或
        #    def scope_active(self, query)）
        scope_name = f"scope_{snake}"
        raw_scope = None
        for c in cls.__mro__:
            if scope_name in c.__dict__:
                raw_scope = c.__dict__[scope_name]
                break
        if raw_scope is not None:
            if isinstance(raw_scope, classmethod):
                bound_scope = getattr(cls, scope_name)

                def _scope_proxy(*args: Any, **kwargs: Any) -> "Query":
                    q = cls.query()
                    bound_scope(q, *args, **kwargs)
                    return q
            else:

                def _scope_proxy(*args: Any, **kwargs: Any) -> "Query":
                    q = cls.query()
                    raw_scope(None, q, *args, **kwargs)
                    return q

            _scope_proxy.__name__ = name
            return _scope_proxy
        # 3) 代理到查询构造器
        q = cls.query()
        qattr = getattr(q, snake, None)
        if qattr is not None and callable(qattr) and not snake.startswith("_"):

            def _q_proxy(*args: Any, **kwargs: Any) -> Any:
                return getattr(cls.query(), snake)(*args, **kwargs)

            _q_proxy.__name__ = name
            return _q_proxy
        raise AttributeError(f"type object {cls.__name__!r} has no attribute {name!r}")


class Model(metaclass=MetaModel):
    # ---- 类配置（子类覆盖） ---- #
    __connection__: Any = None      # 连接名或 Connection 对象（默认全局连接）
    __table__: Optional[str] = None  # 表名（默认类名转 snake_case）
    __prefix__: str = ""            # 表前缀
    __pk__: str = "id"              # 主键
    __timestamps__: bool = False    # 自动写入 create_time / update_time
    __create_time__: str = "create_time"
    __update_time__: str = "update_time"
    __soft_delete__: Optional[str] = None  # 软删除字段名，None 表示不使用
    __type__: Dict[str, Any] = {}   # 类型转换 {'id': 'int', 'tags': 'json'}
    __json__: List[str] = []        # JSON 字段（自动序列化/反序列化）
    __fields_cache__: Optional[List[str]] = None  # 表结构缓存（勿手改）

    # ---- 实例属性（object.__setattr__ 直接写入，避免走 __setattr__） ---- #
    def __init__(self, data: Optional[dict] = None, exists: bool = False, **kwargs: Any):
        merged = dict(data or {})
        if kwargs:
            merged.update(kwargs)
        object.__setattr__(self, "_data", {})
        object.__setattr__(self, "_relation", {})
        object.__setattr__(self, "_exists", bool(exists))
        object.__setattr__(self, "_changed", set())
        object.__setattr__(self, "_hidden", set())
        object.__setattr__(self, "_visible", None)
        object.__setattr__(self, "_rel_cache", {})
        object.__setattr__(self, "_together", [])
        # 逐字段调用 set_attr，确保 set_xxx_attr 修改器对构造数据同样生效
        # （从数据库读取走 _from_data，不经过本方法，不会被重复加工）
        if merged:
            self.set_attrs(merged)
            if exists:
                # 已存在记录：视为无变更，避免首次 save 重写全部字段
                object.__setattr__(self, "_changed", set())

    # ------------------------------------------------------------------ #
    # 属性访问（数据字段 / 已加载关联 / 关联懒加载 / 获取器）
    # ------------------------------------------------------------------ #
    def __getattribute__(self, name: str) -> Any:
        if name.startswith("_"):
            return object.__getattribute__(self, name)
        try:
            data = object.__getattribute__(self, "_data")
        except AttributeError:
            return object.__getattribute__(self, name)
        if name in data:
            return self.get_attr(name)
        try:
            rel = object.__getattribute__(self, "_relation")
            if name in rel:
                return rel[name]
        except AttributeError:
            pass
        # 关联方法懒加载（带缓存，仅对【用户子类自定义】的方法生效一次，
        # Model 基类方法如 save/delete/to_dict 不参与探测，避免双重执行）
        cls = type(self)
        method = None
        for c in cls.__mro__:
            if name in c.__dict__ and callable(c.__dict__[name]):
                if c is Model:
                    break  # 基类方法：直接走默认属性访问
                method = c.__dict__[name]
                break
        if method is not None and not name.startswith(_GETTER_PREFIXES):
            try:
                cache = object.__getattribute__(self, "_rel_cache")
            except AttributeError:
                cache = None
            if cache is not None:
                if name in cache:
                    if cache[name]:
                        return self._load_relation(name)
                else:
                    try:
                        result = method(self)
                    except Exception:
                        result = None
                    if isinstance(result, Relation):
                        cache[name] = True
                        result.name = name
                        return self._load_relation(result)
                    cache[name] = False
        return object.__getattribute__(self, name)

    def __getattr__(self, name: str) -> Any:
        # 获取器兜底（字段不存在时）
        getter = getattr(type(self), f"get_{name}_attr", None)
        if getter is not None:
            return getter(self, None)
        # 空模型/未加载字段：对齐 think-orm 语义返回 None（仅普通字段名，
        # 避免掩盖明显的拼写错误）
        if name and (name[0].isalnum() or name[0] == "_") and not name.endswith("__"):
            return None
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        # 仅当 name 是【用户子类自定义】的方法（MRO 直查，排除 Model 基类方法与元类代理）
        method = None
        for c in type(self).__mro__:
            if name in c.__dict__ and callable(c.__dict__[name]):
                if c is Model:
                    break
                method = c.__dict__[name]
                break
        if method is not None:
            # 关联方法赋值 -> 存入关联数据（配合 together() 级联保存）
            if isinstance(value, (Model, list, tuple, Collection)) and name not in self._data:
                try:
                    result = method(self)
                except Exception:
                    result = None
                if isinstance(result, Relation):
                    self._relation[name] = value
                    return
            object.__setattr__(self, name, value)
            return
        self.set_attr(name, value)

    def __getitem__(self, key: str) -> Any:
        return self.get_attr(key)

    def __setitem__(self, key: str, value: Any) -> None:
        self.set_attr(key, value)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __iter__(self):
        return iter(self._data.keys())

    # ------------------------------------------------------------------ #
    # 属性读写（含类型转换与获取器/修改器）
    # ------------------------------------------------------------------ #
    def set_attr(self, name: str, value: Any) -> None:
        """设置属性（应用 set_xxx_attr 修改器，记录变更）。"""
        setter = getattr(type(self), f"set_{name}_attr", None)
        if setter is not None:
            value = setter(self, value)
        self._data[name] = value
        self._changed.add(name)

    def set_attrs(self, data: dict) -> "Model":
        for k, v in (data or {}).items():
            self.set_attr(k, v)
        return self

    def get_attr(self, name: str) -> Any:
        """读取属性（类型反序列化 + get_xxx_attr 获取器）。"""
        if name not in self._data:
            return None
        value = self._deserialize(name, self._data[name])
        getter = getattr(type(self), f"get_{name}_attr", None)
        if getter is not None:
            value = getter(self, value)
        return value

    # ------------------------------------------------------------------ #
    # 类型转换
    # ------------------------------------------------------------------ #
    def _serialize(self, field: str, value: Any) -> Any:
        t = self.__type__.get(field)
        if t in ("json", "array") or field in self.__json__:
            return json.dumps(value, ensure_ascii=False) if value is not None else None
        if t == "bool" and isinstance(value, bool):
            return 1 if value else 0
        if t in ("datetime", "date") and isinstance(value, (datetime, date)):
            return value.strftime("%Y-%m-%d %H:%M:%S" if t == "datetime" else "%Y-%m-%d")
        if t == "time" and isinstance(value, dtime):
            return value.strftime("%H:%M:%S")
        return value

    def _deserialize(self, field: str, value: Any) -> Any:
        if value is None:
            return None
        t = self.__type__.get(field)
        if t in ("json", "array") or field in self.__json__:
            # 查询层（Query.json / 模型 __json__ 自动接入）可能已解码，
            # 对 dict / list 幂等返回，避免二次 json.loads 报错或复制
            if isinstance(value, (dict, list)):
                return value
            try:
                return json.loads(value)
            except (ValueError, TypeError):
                return value
        try:
            if t == "int":
                return int(value)
            if t == "float":
                return float(value)
            if t == "bool":
                return bool(int(value))
            if t == "datetime":
                return datetime.fromisoformat(str(value))
            if t == "date":
                return date.fromisoformat(str(value)[:10])
            if t == "time":
                return dtime.fromisoformat(str(value))
        except ValueError:
            return value
        return value

    # ------------------------------------------------------------------ #
    # 连接 / 表
    # ------------------------------------------------------------------ #
    @classmethod
    def _connection(cls):
        from . import Db
        return Db.get_connection(cls.__connection__)

    @classmethod
    def _table_name(cls) -> str:
        if cls.__table__:
            return cls.__table__
        return to_snake(cls.__name__)

    @classmethod
    def _get_fields(cls) -> Optional[List[str]]:
        if cls.__fields_cache__ is None:
            try:
                cls.__fields_cache__ = cls._connection().table_fields(cls._table_name())
            except Exception:
                cls.__fields_cache__ = None
        return cls.__fields_cache__

    @classmethod
    def _has_field(cls, field: str) -> bool:
        fields = cls._get_fields()
        return True if fields is None else field in fields

    @classmethod
    def _now(cls) -> str:
        from datetime import datetime as _dt
        return _dt.now().strftime("%Y-%m-%d %H:%M:%S")

    @classmethod
    def query(cls) -> "Query":
        """获取绑定当前模型的查询构造器。"""
        from .query import Query
        q = Query(cls._connection(), prefix=cls.__prefix__, model=cls)
        q.name(cls._table_name())   # name() 会自动补 __prefix__（前缀为空时等价 table()）
        if cls.__soft_delete__:
            q.options["soft_delete"] = cls.__soft_delete__
        # __json__ 声明的字段：查询结果自动解码为 dict / list（v0.5.0），
        # 使 User.find(1).extra 直接拿到 Python 对象而非 JSON 文本
        if cls.__json__:
            q.options["json"] = list(cls.__json__)
        return q

    @classmethod
    def _from_data(cls, data: dict) -> "Model":
        """由数据库行构造模型实例（不做修改器/变更跟踪）。"""
        m = cls()
        object.__setattr__(m, "_data", dict(data or {}))
        object.__setattr__(m, "_exists", True)
        return m

    # ------------------------------------------------------------------ #
    # 静态查询入口
    # ------------------------------------------------------------------ #
    @classmethod
    def find(cls, id: Optional[Any] = None) -> Optional["Model"]:
        return cls.query().find(id)

    @classmethod
    def get(cls, id: Optional[Any] = None) -> Optional["Model"]:
        """think-orm get()，等价 find()。"""
        return cls.query().find(id)

    @classmethod
    def find_or_empty(cls, id: Optional[Any] = None) -> Optional["Model"]:
        return cls.query().find_or_empty(id)

    @classmethod
    def find_or_fail(cls, id: Optional[Any] = None) -> "Model":
        return cls.query().find_or_fail(id)

    @classmethod
    def select(cls) -> Collection:
        return cls.query().select()

    @classmethod
    def select_or_fail(cls) -> Collection:
        return cls.query().select_or_fail()

    @classmethod
    def value(cls, field: str, default: Any = None) -> Any:
        return cls.query().value(field, default)

    @classmethod
    def column(cls, field: str, key: Optional[str] = None) -> Any:
        return cls.query().column(field, key)

    @classmethod
    def count(cls, field: str = "*") -> int:
        return cls.query().count(field)

    @classmethod
    def sum(cls, field: str) -> Any:
        return cls.query().sum(field)

    @classmethod
    def avg(cls, field: str) -> float:
        return cls.query().avg(field)

    @classmethod
    def max(cls, field: str) -> Any:
        return cls.query().max(field)

    @classmethod
    def min(cls, field: str) -> Any:
        return cls.query().min(field)

    @classmethod
    def paginate(cls, list_rows: int = 15, page: Optional[int] = None):
        return cls.query().paginate(list_rows, page)

    # ------------------------------------------------------------------ #
    # 静态写入
    # ------------------------------------------------------------------ #
    @classmethod
    def create(cls, data: dict) -> "Model":
        """创建并保存记录（应用 set_xxx_attr 修改器）。"""
        m = cls(data)      # 走 __init__ → set_attrs，修改器生效
        m.save()
        return m

    @classmethod
    def update(cls, data: dict, where: Any = None) -> int:
        """按条件更新: User.update({'name': 'x'}, {'id': 1})。"""
        q = cls.query()
        if where is not None:
            if callable(where):
                where(q)
            elif isinstance(where, dict):
                q.where(where)
            elif isinstance(where, (list, tuple)) and len(where) >= 2 and not isinstance(where[0], dict):
                q.where(*where)
            else:
                q.where(cls.__pk__, where)
        if cls.__timestamps__ and cls.__update_time__:
            data = dict(data)
            if cls._has_field(cls.__update_time__) and cls.__update_time__ not in data:
                data[cls.__update_time__] = cls._now()
        return q.update(data)

    @classmethod
    def destroy(cls, condition: Any) -> int:
        """删除：destroy(1) / destroy([1,2,3]) / destroy({'status': 0}) / destroy(closure)。

        启用软删除时执行逻辑删除。
        """
        q = cls.query()
        if callable(condition):
            condition(q)
        elif isinstance(condition, (list, tuple)) and not any(isinstance(x, dict) for x in condition):
            q.where_in(cls.__pk__, condition)
        elif isinstance(condition, dict):
            q.where(condition)
        else:
            q.where(cls.__pk__, condition)
        if cls.__soft_delete__:
            return q.update({cls.__soft_delete__: cls._now()})
        return q.delete()

    @classmethod
    def with_trashed(cls) -> "Query":
        """包含软删除数据。"""
        q = cls.query()
        q.options["soft_delete"] = None
        return q

    @classmethod
    def only_trashed(cls) -> "Query":
        """仅查询已软删除数据。"""
        q = cls.query()
        q.options["soft_delete"] = None
        q.where(cls.__soft_delete__, "is not", None)
        return q

    # ------------------------------------------------------------------ #
    # 实例持久化
    # ------------------------------------------------------------------ #
    def save(self, data: Optional[dict] = None) -> int:
        """保存：新记录插入，已存在记录更新。返回影响行数/自增主键。"""
        if data:
            self.set_attrs(data)
        self.on_before_write()
        try:
            if self._exists:
                result = self._do_update()
            else:
                result = self._do_insert()
            self._save_together()
            return result
        finally:
            self.on_after_write()

    def _do_insert(self) -> int:
        data = {}
        for k, v in self._data.items():
            data[k] = self._serialize(k, v)
        if self.__timestamps__:
            now = self._now()
            for tf in (self.__create_time__, self.__update_time__):
                if self._has_field(tf) and tf not in data:
                    data[tf] = now
                    self._data[tf] = now
        self.on_before_insert()
        pk = self.__pk__
        q = type(self).query()
        if pk in data and data[pk] is not None:
            q.insert(data)
        else:
            rid = q.insert(data)
            if rid and self._has_field(pk):
                self._data[pk] = rid
        object.__setattr__(self, "_exists", True)
        self._changed.clear()
        self.on_after_insert()
        return self._data.get(pk)

    def _do_update(self) -> int:
        pk = self.__pk__
        if pk not in self._data or self._data[pk] is None:
            raise QueryError(f"更新 {type(self).__name__} 记录缺少主键值")
        data = {}
        for f in self._changed:
            if f == pk:
                continue
            data[f] = self._serialize(f, self._data[f])
        if self.__timestamps__ and self.__update_time__:
            if self._has_field(self.__update_time__):
                data[self.__update_time__] = self._now()
        if not data:
            return 0
        self.on_before_update()
        n = type(self).query().where(pk, self._data[pk]).update(data)
        self._changed.clear()
        self.on_after_update()
        return n

    def delete(self) -> int:
        """删除记录（软删除开启时执行逻辑删除）。"""
        pk = self.__pk__
        if pk not in self._data or self._data[pk] is None:
            raise QueryError(f"删除 {type(self).__name__} 记录缺少主键值")
        self.on_before_delete()
        q = type(self).query()
        if self.__soft_delete__:
            n = q.where(pk, self._data[pk]).update({self.__soft_delete__: self._now()})
        else:
            n = q.where(pk, self._data[pk]).delete()
        self.on_after_delete()
        return n

    def force_delete(self) -> int:
        """物理删除（无视软删除）。"""
        pk = self.__pk__
        if pk not in self._data or self._data[pk] is None:
            raise QueryError("缺少主键值")
        q = type(self).query()
        q.options["soft_delete"] = None
        return q.where(pk, self._data[pk]).delete()

    def restore(self) -> int:
        """恢复软删除记录。"""
        if not self.__soft_delete__:
            return 0
        q = type(self).query()
        q.options["soft_delete"] = None  # 恢复需无视软删除过滤
        return q.where(self.__pk__, self._data.get(self.__pk__)) \
            .update({self.__soft_delete__: None})

    # ------------------------------------------------------------------ #
    # 关联
    # ------------------------------------------------------------------ #
    def has_one(self, target, foreign_key=None, local_key=None) -> Relation:
        return Relation(self, target, HAS_ONE, foreign_key, local_key)

    def has_many(self, target, foreign_key=None, local_key=None) -> Relation:
        return Relation(self, target, HAS_MANY, foreign_key, local_key)

    def belongs_to(self, target, foreign_key=None, local_key=None) -> Relation:
        return Relation(self, target, BELONGS_TO, foreign_key, local_key)

    def belongs_to_many(self, target, middle=None, foreign_key=None, local_key=None) -> Relation:
        return Relation(self, target, BELONGS_TO_MANY, foreign_key, local_key, middle)

    @classmethod
    def _get_relation(cls, name: str, instance: "Model") -> Relation:
        """获取关联对象（预载入用）。"""
        method = getattr(cls, name, None)
        if method is None or not callable(method):
            raise RelationNotFound(f"模型 {cls.__name__} 未定义关联方法 {name}()")
        rel = method(instance)
        if not isinstance(rel, Relation):
            raise RelationNotFound(f"{cls.__name__}.{name} 不是关联方法（需返回 has_one/has_many/...）")
        rel.name = name
        return rel

    def _load_relation(self, name: Any) -> Any:
        """懒加载关联数据并缓存。name 可为关联名或 Relation 对象。"""
        if isinstance(name, Relation):
            rel = name
        else:
            rel = type(self)._get_relation(name, self)
        data = rel.get_result(self)
        self._relation[rel.name] = data
        return data

    def get_relation(self, name: str) -> Any:
        """显式获取关联数据（等价属性访问）。"""
        return self._load_relation(name)

    def together(self, relations) -> "Model":
        """保存主模型时级联保存关联: user.together(['profile']).save()。"""
        rels = relations if isinstance(relations, (list, tuple)) else [relations]
        object.__setattr__(self, "_together", list(rels))
        return self

    def _save_together(self) -> None:
        for name in self._together:
            rel = type(self)._get_relation(name, self)
            data = self._relation.get(name)
            if data is None:
                continue
            if isinstance(data, Model):
                self._save_relation_model(rel, data)
            elif isinstance(data, (list, Collection)):
                for sub in data:
                    if isinstance(sub, Model):
                        self._save_relation_model(rel, sub)

    def _save_relation_model(self, rel: Relation, sub: Model) -> None:
        if rel.type in (HAS_ONE, HAS_MANY):
            # 关联表外键指向自己
            sub.set_attr(rel.foreign_key, self.get_attr(rel.local_key))
            sub.save()
        elif rel.type == BELONGS_TO:
            # 自己外键指向关联模型
            self.set_attr(rel.foreign_key, sub.get_attr(rel.local_key))
            self.save()

    # ------------------------------------------------------------------ #
    # 输出
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict:
        """转为普通 dict（含关联数据）。"""
        out = {}
        for k in self._data:
            if self._hidden and k in self._hidden:
                continue
            if self._visible is not None and k not in self._visible:
                continue
            out[k] = self.get_attr(k)
        for k, v in self._relation.items():
            out[k] = _rel_to_py(v)
        return out

    def to_array(self) -> dict:
        return self.to_dict()

    def to_json(self, **kwargs: Any) -> str:
        # default=str：让 date/datetime/time 等类型转换字段可序列化为 ISO 字符串
        kwargs.setdefault("default", str)
        return json.dumps(self.to_dict(), ensure_ascii=False, **kwargs)

    def hidden(self, *fields: str) -> "Model":
        self._hidden.update(fields)
        return self

    def visible(self, *fields: str) -> "Model":
        object.__setattr__(self, "_visible", set(fields))
        return self

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"{type(self).__name__}({self._data!r})"

    # ------------------------------------------------------------------ #
    # 模型事件（子类覆盖）
    # ------------------------------------------------------------------ #
    def on_before_write(self) -> None: pass
    def on_after_write(self) -> None: pass
    def on_before_insert(self) -> None: pass
    def on_after_insert(self) -> None: pass
    def on_before_update(self) -> None: pass
    def on_after_update(self) -> None: pass
    def on_before_delete(self) -> None: pass
    def on_after_delete(self) -> None: pass


def _rel_to_py(value: Any) -> Any:
    if isinstance(value, Model):
        return value.to_dict()
    if isinstance(value, (list, Collection)):
        return [_rel_to_py(v) for v in value]
    return value
