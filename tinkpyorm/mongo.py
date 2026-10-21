"""MongoDB 集合数据访问层（v0.12.0）。

本模块是 MongoDB 的**服务层**：驱动层（:mod:`tinkpyorm.drivers.mongodb`）
只负责连接、版本门禁与异常翻译；集合的增删改查、输入校验、索引与
聚合封装都在这里。

与项目内既有风格的一致性
========================

* **操作符沿用 think-orm 链式词汇**，不用 MongoDB 的 ``$`` 字典风格
  （与 :class:`tinkpyorm.document.DocumentCollection` 的既定决策一致），
  需要原生语法时用 :meth:`MongoCollection.where_raw` 逃生舱
  （对应 ``Query.where_raw``）。
* **链式累积 + 终端复位**：链式方法（where / order / limit / page）累积
  状态，终端方法（select / insert / update / delete）执行后复位，
  同一实例可连续发起多个独立操作。
* **异常归口**：所有 ``pymongo`` / ``bson`` 异常在驱动层翻译为项目异常
  体系（``OrmError`` 子类），本层只补充**输入校验**类错误。

基本用法::

    from tinkpyorm import Db

    Db.config({"type": "mongodb", "database": "app",
               "options": {"uri": "mongodb://127.0.0.1:27017"}})

    users = Db.mongo("users")
    uid = users.insert({"name": "张三", "age": 25, "tags": ["vip"]})
    users.find_by_id(uid)
    users.where("age", ">=", 18).order("age", "desc").limit(10).select()
    users.where("name", "张三").update({"age": 26})
    users.where("age", "<", 18).delete()

性能要点（详见 ``wiki/30-MongoDB支持.md``）：

* 常用查询字段务必 :meth:`MongoCollection.ensure_index` 建索引，
  否则 ``where`` 走 COLLSCAN（可用 :meth:`MongoCollection.explain` 自查）；
* 查询一律用 :meth:`MongoCollection.field` 做投影，并显式
  :meth:`MongoCollection.limit`，避免拉回整文档；
* 批量写入用 :meth:`MongoCollection.insert_many` / :meth:`bulk_write`
  （底层单次网络往返），不要循环单条插入；
* 关联查询用 :meth:`MongoCollection.aggregate` 的 ``$lookup``，
  或 :meth:`MongoCollection.where_in` 批量取，避免 N+1；
* 深分页用 :meth:`MongoCollection.after` 的游标（keyset）方式，
  偏移量分页在百万级集合上会因 ``skip`` 扫描而线性变慢。
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .builder import JSON_OPS
from .collection import Collection
from .drivers.mongodb import translate_mongo_error
from .exceptions import InvalidArgumentException, OrmError, QueryError
from .utils import UNSET

__all__ = ["MongoCollection", "MONGO_OPS"]

_UNSET = UNSET

#: MongoDB 集合层沿用的操作符词汇（与 Query / DocumentCollection 同一套）。
MONGO_OPS = frozenset(JSON_OPS)

#: 合法集合名（字母 / 数字 / 下划线，不以数字开头）。
_COLLECTION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 合法字段名（非空、不以 ``$`` 开头、不含空字符）。
_FIELD_RE = re.compile(r"^[^$\x00][^\x00]*$")

#: ObjectId 的十六进制形态（24 位）。
_OID_RE = re.compile(r"^[0-9a-fA-F]{24}$")

#: 默认单次查询返回条数上限（防止误查全表）。
DEFAULT_MAX_LIMIT = 1000

#: 分页每页最大条数。
MAX_PAGE_SIZE = 1000


def _is_op_dict(value: Any) -> bool:
    """判断是否为 MongoDB 算子字典（键全部以 ``$`` 开头）。"""
    return isinstance(value, dict) and bool(value) and all(
        str(k).startswith("$") for k in value)


def _like_to_regex(pattern: str) -> Any:
    """SQL ``LIKE`` 模式转正则（``%`` -> ``.*``，``_`` -> ``.``，大小写不敏感）。

    与 SQLite 的 ``LIKE`` 语义对齐（ASCII 大小写不敏感），并对正则元字符
    做转义，避免用户输入被当作正则语法执行。
    """
    out = []
    for ch in str(pattern):
        if ch == "%":
            out.append(".*")
        elif ch == "_":
            out.append(".")
        else:
            out.append(re.escape(ch))
    return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


class MongoCollection:
    """MongoDB 集合的读写入口。

    实例由 :meth:`tinkpyorm.db.Db.mongo` 创建。链式方法累积查询意图，
    终端方法执行后复位（同一实例可连续发起多个独立操作）。
    """

    def __init__(self, conn: Any, name: str, prefix: str = "",
                 write_concern: Optional[Dict[str, Any]] = None):
        if not isinstance(name, str) or not _COLLECTION_RE.match(name.strip()):
            raise InvalidArgumentException(
                f"非法集合名: {name!r}（须为字母/数字/下划线，且不以数字开头）")
        driver = getattr(conn, "driver", None)
        if driver is not None and not getattr(driver, "supports_mongo", False):
            from .drivers import UnsupportedOperation
            raise UnsupportedOperation(
                f"{getattr(driver, 'name', '当前')} 驱动不支持 MongoDB 集合："
                f"该功能依赖真实 MongoDB 服务端（pymongo），"
                f"请改用 Db.table() 链式查询，或为该驱动实现 Mongo 访问层")
        self.conn = conn
        self.name = name.strip()
        self.prefix = str(prefix or "")
        self.collection_name = self.prefix + self.name
        self._write_concern = dict(write_concern) if write_concern else None
        # 链式状态
        self._and_conditions: List[Dict[str, Any]] = []
        self._or_conditions: List[Dict[str, Any]] = []
        self._sort: List[Tuple[str, int]] = []
        self._projection: Optional[Dict[str, int]] = None
        self._limit: Optional[int] = None
        self._skip: int = 0
        # schema 校验
        self._schema: Dict[str, Any] = {}
        self._required: List[str] = []

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    def _driver(self) -> Any:
        return self.conn.driver

    def _write_concern_obj(self) -> Any:
        """把 ``{w, j, wtimeout}`` 字典转成 pymongo 的 ``WriteConcern``。

        pymongo 的 ``get_collection(write_concern=...)`` **只接受**
        ``WriteConcern`` 实例，传 dict 会抛 ``TypeError``——因此这里做一次
        归一；未知键明确报错（与驱动层 ``write_concern`` 的规则一致）。
        """
        if not self._write_concern:
            return None
        from .drivers.mongodb import MONGO_WRITE_CONCERN_KEYS
        # 先校验键名（不依赖 pymongo，便于离线测试覆盖）
        kwargs: Dict[str, Any] = {}
        for key, value in self._write_concern.items():
            target = MONGO_WRITE_CONCERN_KEYS.get(str(key).lower())
            if target is None:
                raise InvalidArgumentException(
                    f"未知的写关注参数: {key!r}。可选: "
                    + ", ".join(sorted(set(MONGO_WRITE_CONCERN_KEYS))))
            # WriteConcern 的参数名是 wtimeout（不是 wtimeoutMS）
            kwargs["wtimeout" if target == "wtimeoutMS" else target] = value
        try:
            from pymongo.write_concern import WriteConcern
        except ImportError as exc:  # pragma: no cover - 取决于环境
            raise InvalidArgumentException(
                "设置 write_concern 需要 pymongo（或 pip install tinkpyorm[mongo]）"
            ) from exc
        return WriteConcern(**kwargs)

    def _raw(self) -> Any:
        """底层 ``pymongo.Collection``。"""
        driver = self._driver()
        concern = self._write_concern_obj()
        if concern is not None:
            return driver.database().get_collection(self.collection_name,
                                                   write_concern=concern)
        return driver.collection(self.collection_name)

    @staticmethod
    def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
        """执行一次 pymongo 调用，并把第三方异常翻译成项目异常。

        统一走这一个入口，避免逐个调用点漏翻译——漏一个就会让
        ``pymongo.errors.*`` 泄漏到调用方，破坏"异常归口"的不变量。
        """
        try:
            return fn(*args, **kwargs)
        except OrmError:
            raise
        except Exception as exc:
            raise translate_mongo_error(exc) from exc

    def _exec(self, method: str, *args: Any, **kwargs: Any) -> Any:
        """在底层集合上调用 ``method`` 并翻译异常。

        注意 ``_raw()`` 也在翻译范围内——集合的**构造**同样会抛异常
        （如 ``write_concern`` 类型错误、连接建立失败），不能只包住调用本身。
        """
        coll = self._call(self._raw)
        return self._call(getattr(coll, method), *args, **kwargs)

    def _finish(self, result: Any) -> Any:
        """终端收尾：复位链式状态。"""
        self._and_conditions = []
        self._or_conditions = []
        self._sort = []
        self._projection = None
        self._limit = None
        self._skip = 0
        return result

    @staticmethod
    def _check_field(field: str) -> str:
        """校验字段名（MongoDB 顶层键不能以 ``$`` 开头，也不能为空）。"""
        if not isinstance(field, str) or not field.strip():
            raise InvalidArgumentException(f"非法字段名: {field!r}")
        f = field.strip()
        if not _FIELD_RE.match(f):
            raise InvalidArgumentException(
                f"非法字段名: {field!r}（不能为空、以 $ 开头或含空字符）")
        return f

    def _build_filter(self) -> Dict[str, Any]:
        """把累积的条件合并为 MongoDB 过滤文档。

        同一 AND 组的同一字段会合并进同一个算子字典
        （``{"$gte":..,"$lte":..}``），以便命中复合索引；无法合并时退回
        ``$and``。

        OR 条件单独成组（``{"$or": [...]}``），再与 AND 组用 ``$and``
        连接——避免"两个 OR 条件被 AND 起来"改变语义。
        """
        flat: Dict[str, Any] = {}
        extras: List[Dict[str, Any]] = []
        for cond in self._and_conditions:
            for field, expr in cond.items():
                if field not in flat:
                    flat[field] = expr
                    continue
                old = flat[field]
                if _is_op_dict(old) and _is_op_dict(expr):
                    merged = dict(old)
                    conflict = False
                    for k, v in expr.items():
                        if k in merged and merged[k] != v:
                            conflict = True
                        merged[k] = v
                    if not conflict:
                        flat[field] = merged
                        continue
                extras.append({field: expr})
        and_part: Optional[Dict[str, Any]] = None
        if flat or extras:
            and_part = ({"$and": ([flat] if flat else []) + extras}
                        if extras else flat)
        if self._or_conditions:
            or_part = {"$or": list(self._or_conditions)}
            if and_part is None:
                return or_part
            return {"$and": [and_part, or_part]}
        return and_part or {}

    def _build_sort(self) -> Optional[List[Tuple[str, int]]]:
        return list(self._sort) if self._sort else None

    def _effective_limit(self) -> int:
        """本次查询实际生效的条数上限。"""
        if self._limit is not None:
            return self._limit
        driver = self._driver()
        configured = getattr(driver.config, "options", {}).get("max_limit")
        try:
            cap = int(configured) if configured else DEFAULT_MAX_LIMIT
        except (TypeError, ValueError):
            cap = DEFAULT_MAX_LIMIT
        return cap

    def _validate_document(self, doc: Any, partial: bool = False) -> None:
        """文档字段校验（类型 + 必填）。"""
        if not isinstance(doc, dict):
            raise InvalidArgumentException(
                f"文档必须是 dict，收到: {type(doc).__name__}")
        for key in doc:
            if not isinstance(key, str) or not key.strip():
                raise InvalidArgumentException(f"非法字段名: {key!r}")
            if key.startswith("$"):
                raise InvalidArgumentException(
                    f"字段名不能以 $ 开头（MongoDB 保留前缀）: {key!r}")
        if not partial:
            for field in self._required:
                if field not in doc:
                    raise InvalidArgumentException(f"缺少必填字段: {field!r}")
        for field, typ in self._schema.items():
            if field not in doc or doc[field] is None:
                continue
            self._check_type(field, doc[field], typ)

    @staticmethod
    def _check_type(field: str, value: Any, typ: Any) -> None:
        """类型校验（bool 与 int/float 互不兼容，float 宽容接受 int）。"""
        if typ is bool:
            ok = isinstance(value, bool)
        elif typ is int:
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif typ is float:
            ok = (isinstance(value, (int, float))
                  and not isinstance(value, bool))
        else:
            ok = isinstance(value, typ)
        if not ok:
            raise InvalidArgumentException(
                f"字段 {field!r} 的值类型不符：声明 {typ.__name__}，"
                f"实际 {type(value).__name__}")

    @staticmethod
    def _to_object_id(value: Any) -> Any:
        """把 24 位十六进制字符串转成 ``ObjectId``；其它值原样返回。

        ``pymongo`` 未安装时抛明确错误（而不是在深层抛 ImportError）。
        """
        if isinstance(value, str) and _OID_RE.match(value.strip()):
            try:
                from bson import ObjectId
            except ImportError as exc:  # pragma: no cover - 取决于环境
                raise InvalidArgumentException(
                    "转换 ObjectId 需要 bson（随 pymongo 一起安装）") from exc
            return ObjectId(value.strip())
        return value

    # ------------------------------------------------------------------ #
    # 条件（think-orm 操作符词汇）
    # ------------------------------------------------------------------ #
    def where(self, field: str, op: Any = _UNSET,
              value: Any = _UNSET) -> "MongoCollection":
        """条件（AND 语义）。运算符与 Query / DocumentCollection 同一套词汇::

            users.where("age", ">=", 18)
            users.where("name", "张三")            # 省略运算符 = 等于
            users.where("tags", ["vip", "beta"])   # 值为列表 = IN
            users.where("deleted", None)           # 为 null 或字段缺失
            users.where("name", "exists")          # 字段存在
            users.where("tags", "contains", "vip") # 数组成员包含

        完整运算符见 :data:`tinkpyorm.mongo.MONGO_OPS`。
        """
        return self._cond("AND", field, op, value)

    def where_or(self, field: str, op: Any = _UNSET,
                 value: Any = _UNSET) -> "MongoCollection":
        """OR 条件的条件，签名同 :meth:`where`。"""
        return self._cond("OR", field, op, value)

    def _cond(self, logic: str, field: Any, op: Any, value: Any
              ) -> "MongoCollection":
        if isinstance(field, dict):
            for k, v in field.items():
                self._cond(logic, k, _UNSET, v)
            return self
        # 2 参形式归一：第二参不是运算符时视为值
        if op is not _UNSET and value is _UNSET and not (
                isinstance(op, str) and op.strip().lower() in MONGO_OPS):
            op, value = _UNSET, op
        name = self._check_field(field)
        if op is _UNSET:
            if isinstance(value, (list, tuple, set)) and \
                    not isinstance(value, (str, bytes)):
                op, value = "in", list(value)
            else:
                op = "="
        if not (isinstance(op, str) and op.strip().lower() in MONGO_OPS):
            raise QueryError(
                f"不支持的条件运算符: {op!r}。可选: "
                + ", ".join(sorted(MONGO_OPS)))
        expr = self._expr_for(op.strip().lower(), value)
        cond = {name: expr}
        if logic == "OR":
            self._or_conditions.append(cond)
        else:
            self._and_conditions.append(cond)
        return self

    @staticmethod
    def _expr_for(op: str, value: Any) -> Any:
        """操作符 -> MongoDB 查询表达式。

        映射要点：

        ==========================  ==================================
        think-orm 运算符            MongoDB 表达式
        ==========================  ==================================
        ``=``                       ``{field: value}``
        ``!=`` / ``<>``             ``{field: {"$ne": value}}``
        ``>`` ``>=`` ``<`` ``<=``  ``$gt`` / ``$gte`` / ``$lt`` / ``$lte``
        ``in`` / ``not in``         ``$in`` / ``$nin``
        ``between``                 ``{"$gte": lo, "$lte": hi}``
        ``like`` / ``not like``     正则（``%``/``_`` 通配，大小写不敏感）
        ``null`` / ``not null``     ``{field: None}`` / ``{"$ne": None}``
        ``exists`` / ``not exists`` ``$exists: True`` / ``False``
        ``contains``                ``{field: value}``（数组含元素）
        ==========================  ==================================
        """
        if op == "=":
            return value
        if op in ("!=", "<>"):
            return {"$ne": value}
        if op == ">":
            return {"$gt": value}
        if op == ">=":
            return {"$gte": value}
        if op == "<":
            return {"$lt": value}
        if op == "<=":
            return {"$lte": value}
        if op == "in":
            if not isinstance(value, (list, tuple, set)):
                raise QueryError(f"in 条件的值必须是列表，收到: {value!r}")
            return {"$in": list(value)}
        if op == "not in":
            if not isinstance(value, (list, tuple, set)):
                raise QueryError(f"not in 条件的值必须是列表，收到: {value!r}")
            return {"$nin": list(value)}
        if op == "between":
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise QueryError(
                    f"between 条件的值必须是 [下限, 上限]，收到: {value!r}")
            return {"$gte": value[0], "$lte": value[1]}
        if op == "not between":
            if not isinstance(value, (list, tuple)) or len(value) != 2:
                raise QueryError(
                    f"not between 条件的值必须是 [下限, 上限]，收到: {value!r}")
            return {"$not": {"$gte": value[0], "$lte": value[1]}}
        if op == "like":
            return {"$regex": _like_to_regex(value)}
        if op == "not like":
            return {"$not": _like_to_regex(value)}
        if op in ("null", "is null"):
            return None
        if op in ("not null", "is not null"):
            return {"$ne": None}
        if op == "exists":
            return {"$exists": True}
        if op == "not exists":
            return {"$exists": False}
        if op == "contains":
            # MongoDB 的 {field: value} 对数组字段即"含该元素"
            return value
        if op == "not contains":
            return {"$ne": value}
        raise QueryError(f"不支持的条件运算符: {op!r}")

    def where_raw(self, filter_doc: Dict[str, Any]) -> "MongoCollection":
        """原生 MongoDB 过滤文档（逃生舱）::

            users.where_raw({"$expr": {"$gt": ["$balance", 100]}})
            users.where_raw({"$text": {"$search": "关键字"}})

        文档必须是 dict；与链式条件以 ``$and`` 合并。
        """
        if not isinstance(filter_doc, dict) or not filter_doc:
            raise InvalidArgumentException(
                "where_raw 需要非空 dict 形式的 MongoDB 过滤文档")
        self._and_conditions.append(dict(filter_doc))
        return self

    def where_in(self, field: str, values: Sequence[Any]) -> "MongoCollection":
        """``IN`` 条件（批量取数，避免 N+1）::

            users.where_in("_id", [id1, id2, id3])
        """
        return self.where(field, "in", list(values))

    def where_null(self, field: str) -> "MongoCollection":
        """字段为 null 或缺失。"""
        return self.where(field, "null")

    def where_exists(self, field: str) -> "MongoCollection":
        """字段存在。"""
        return self.where(field, "exists")

    # ------------------------------------------------------------------ #
    # 排序 / 投影 / 分页
    # ------------------------------------------------------------------ #
    def order(self, field: str, direction: Optional[str] = None
              ) -> "MongoCollection":
        """排序：``order("age", "desc")``；可多次调用形成复合排序。"""
        name = self._check_field(field)
        if direction is None:
            raw = 1
        elif isinstance(direction, int) and direction in (1, -1):
            raw = direction
        else:
            text = str(direction).strip().lower()
            if text in ("asc", "ascending", "ascend"):
                raw = 1
            elif text in ("desc", "descending", "descend"):
                raw = -1
            else:
                raise InvalidArgumentException(
                    f"非法排序方向: {direction!r}（可选 'asc' / 'desc'）")
        self._sort.append((name, raw))
        return self

    def field(self, *names: str) -> "MongoCollection":
        """字段投影（只返回指定字段）::

            users.field("name", "age").select()
        """
        if not names:
            raise InvalidArgumentException("field 至少需要一个字段名")
        projection = self._projection if self._projection is not None else {}
        for name in names:
            projection[self._check_field(name)] = 1
        self._projection = projection
        return self

    def exclude(self, *names: str) -> "MongoCollection":
        """字段排除（不返回指定字段）。"""
        if not names:
            raise InvalidArgumentException("exclude 至少需要一个字段名")
        projection = self._projection if self._projection is not None else {}
        for name in names:
            projection[self._check_field(name)] = 0
        self._projection = projection
        return self

    def limit(self, length: int, offset: Optional[int] = None
              ) -> "MongoCollection":
        """限制返回条数（``offset`` 为跳过的条数）。"""
        try:
            value = int(length)
        except (TypeError, ValueError):
            raise InvalidArgumentException(f"非法 limit: {length!r}")
        if value < 1:
            raise InvalidArgumentException(f"limit 必须 >= 1，收到: {length!r}")
        self._limit = value
        if offset is not None:
            try:
                skip = int(offset)
            except (TypeError, ValueError):
                raise InvalidArgumentException(f"非法 offset: {offset!r}")
            if skip < 0:
                raise InvalidArgumentException(
                    f"offset 必须 >= 0，收到: {offset!r}")
            self._skip = skip
        return self

    def page(self, page: int, list_rows: int = 20) -> "MongoCollection":
        """偏移量分页（页码从 1 开始）。

        深分页（``page`` 很大）在百万级集合上会因 ``skip`` 扫描而线性变慢，
        此类场景改用 :meth:`after` 的游标（keyset）分页。
        """
        try:
            index = int(page)
            rows = int(list_rows)
        except (TypeError, ValueError):
            raise InvalidArgumentException(
                f"非法分页参数: page={page!r}, list_rows={list_rows!r}")
        if index < 1:
            raise InvalidArgumentException(f"页码必须 >= 1，收到: {page!r}")
        if rows < 1 or rows > MAX_PAGE_SIZE:
            raise InvalidArgumentException(
                f"每页条数须在 1..{MAX_PAGE_SIZE} 之间，收到: {list_rows!r}")
        self._limit = rows
        self._skip = (index - 1) * rows
        return self

    def after(self, last_id: Any) -> "MongoCollection":
        """游标（keyset）分页：只取 ``_id`` 大于 ``last_id`` 的文档。

        相比偏移量分页，代价与页码无关（走 ``_id`` 索引），适合深翻页；
        要求排序字段单调递增（``_id`` 默认满足）。
        """
        self._and_conditions.append({"_id": {"$gt": self._to_object_id(last_id)}})
        return self

    # ------------------------------------------------------------------ #
    # 读取终端
    # ------------------------------------------------------------------ #
    def select(self) -> Collection:
        """执行查询，返回文档列表（:class:`tinkpyorm.collection.Collection`）。"""
        filter_doc = self._build_filter()
        limit = self._effective_limit()
        cursor = self._exec("find", filter_doc,
                            projection=self._projection,
                            sort=self._build_sort(),
                            skip=self._skip,
                            limit=limit)
        return self._finish(Collection([dict(doc) for doc in cursor]))

    def find(self, pk: Optional[Any] = None) -> Optional[dict]:
        """取单条文档。``find(id)`` 按 ``_id`` 查；无结果返回 None。"""
        if pk is not None:
            self._and_conditions.append({"_id": self._to_object_id(pk)})
        cursor = self._exec("find", self._build_filter(),
                            projection=self._projection,
                            sort=self._build_sort(),
                            limit=1)
        for doc in cursor:
            return self._finish(dict(doc))
        return self._finish(None)

    def find_by_id(self, oid: Any) -> Optional[dict]:
        """按 ``_id`` 取单条文档（24 位十六进制字符串自动转 ObjectId）。"""
        return self.find(oid)

    def count(self) -> int:
        """匹配当前条件的文档数。"""
        return self._finish(self._exec("count_documents", self._build_filter()))

    def distinct(self, field: str) -> List[Any]:
        """某字段的去重值列表。"""
        name = self._check_field(field)
        return self._finish(list(self._exec("distinct", name)))

    def aggregate(self, pipeline: Sequence[Dict[str, Any]]) -> Collection:
        """聚合管道（逃生舱，用于 ``$lookup`` 关联、``$group`` 统计等）::

            users.aggregate([
                {"$lookup": {"from": "orders", "localField": "_id",
                             "foreignField": "user_id", "as": "orders"}},
                {"$unwind": "$orders"},
            ])
        """
        if not isinstance(pipeline, (list, tuple)) or not pipeline:
            raise InvalidArgumentException(
                "aggregate 需要非空的管道阶段列表（list[dict]）")
        for stage in pipeline:
            if not isinstance(stage, dict) or not stage:
                raise InvalidArgumentException(
                    f"聚合阶段必须是非空 dict，收到: {stage!r}")
        cursor = self._exec("aggregate", list(pipeline))
        return self._finish(Collection([dict(doc) for doc in cursor]))

    def explain(self) -> Dict[str, Any]:
        """查询执行计划（用于确认是否走了索引）::

            plan = users.where("age", ">=", 18).explain()
            "IXSCAN" in str(plan)   # 命中索引
        """
        filter_doc = self._build_filter()
        command = {
            "explain": {
                "find": self.collection_name,
                "filter": filter_doc,
            },
            "verbosity": "queryPlanner",
        }
        if self._sort:
            command["explain"]["sort"] = dict(self._sort)
        if self._projection:
            command["explain"]["projection"] = self._projection
        if self._limit is not None:
            command["explain"]["limit"] = self._limit
        if self._skip:
            command["explain"]["skip"] = self._skip
        return self._finish(dict(self._call(
            self._driver().database().command, command)))

    # ------------------------------------------------------------------ #
    # 写入终端
    # ------------------------------------------------------------------ #
    def insert(self, doc: Dict[str, Any]) -> Any:
        """插入单条文档，返回 ``_id``。"""
        self._validate_document(doc)
        result = self._exec("insert_one", dict(doc))
        return self._finish(result.inserted_id)

    def insert_many(self, docs: Sequence[Dict[str, Any]]) -> int:
        """批量插入（单次网络往返），返回插入条数。

        大批量数据建议先用 ``ordered=False`` 以跳过单条失败继续插入，
        必要时先写入再按需建索引（建索引期间数据需重排，更慢）。
        """
        if not isinstance(docs, (list, tuple)):
            raise InvalidArgumentException(
                f"insert_many 需要文档列表，收到: {type(docs).__name__}")
        if not docs:
            return self._finish(0)
        for doc in docs:
            self._validate_document(doc)
        result = self._exec("insert_many", [dict(d) for d in docs],
                             ordered=False)
        return self._finish(len(result.inserted_ids))

    def insert_many_ordered(self, docs: Sequence[Dict[str, Any]]) -> int:
        """批量插入（遇错即停），返回插入条数。"""
        if not isinstance(docs, (list, tuple)) or not docs:
            return self._finish(0)
        for doc in docs:
            self._validate_document(doc)
        result = self._exec("insert_many", [dict(d) for d in docs], ordered=True)
        return self._finish(len(result.inserted_ids))

    def update(self, changes: Dict[str, Any], upsert: bool = False) -> int:
        """局部更新（``$set``），返回实际修改条数。

        需先用 :meth:`where` 圈定范围；无匹配且 ``upsert=True`` 时插入新文档。
        """
        self._validate_document(changes, partial=True)
        if not changes:
            raise InvalidArgumentException("update 需要非空的变更字典")
        result = self._exec("update_one", self._build_filter(),
                            {"$set": dict(changes)},
                            upsert=bool(upsert))
        return self._finish(result.modified_count)

    def update_many(self, changes: Dict[str, Any], upsert: bool = False) -> int:
        """批量局部更新（``$set``），返回实际修改条数。"""
        self._validate_document(changes, partial=True)
        if not changes:
            raise InvalidArgumentException("update_many 需要非空的变更字典")
        result = self._exec("update_many", self._build_filter(),
                            {"$set": dict(changes)},
                            upsert=bool(upsert))
        return self._finish(result.modified_count)

    def update_raw(self, update_doc: Dict[str, Any],
                   many: bool = False, upsert: bool = False) -> int:
        """原生更新文档（逃生舱），支持 ``$inc`` / ``$unset`` 等算子::

            users.where("_id", uid).update_raw({"$inc": {"balance": 100}})
            users.where("_id", uid).update_raw({"$unset": {"tmp": ""}})
        """
        if not isinstance(update_doc, dict) or not update_doc:
            raise InvalidArgumentException("update_raw 需要非空的更新文档")
        if not any(str(k).startswith("$") for k in update_doc):
            raise InvalidArgumentException(
                "update_raw 需要以 $ 开头的算子文档（如 {'$inc': {...}}）")
        coll = self._raw()
        if many:
            result = coll.update_many(self._build_filter(), dict(update_doc),
                                      upsert=bool(upsert))
        else:
            result = coll.update_one(self._build_filter(), dict(update_doc),
                                     upsert=bool(upsert))
        return self._finish(result.modified_count)

    def replace(self, doc: Dict[str, Any], upsert: bool = False) -> int:
        """整文档替换（``_id`` 保持不变），返回修改条数。"""
        self._validate_document(doc)
        payload = {k: v for k, v in doc.items() if k != "_id"}
        coll = self._raw()
        result = coll.replace_one(self._build_filter(), payload,
                                  upsert=bool(upsert))
        return self._finish(result.modified_count)

    def delete(self, pk: Optional[Any] = None) -> int:
        """删除单条文档。``delete(id)`` 按 ``_id`` 删；否则按当前条件删。"""
        if pk is not None:
            self._and_conditions.append({"_id": self._to_object_id(pk)})
        result = self._exec("delete_one", self._build_filter())
        return self._finish(result.deleted_count)

    def delete_many(self) -> int:
        """删除所有匹配当前条件的文档（危险操作，慎用）。"""
        result = self._exec("delete_many", self._build_filter())
        return self._finish(result.deleted_count)

    def bulk_write(self, requests: Sequence[Any]) -> Dict[str, Any]:
        """原生批量写（逃生舱），接受 ``pymongo`` 的操作对象列表::

            from pymongo import InsertOne, UpdateOne, DeleteOne
            users.bulk_write([InsertOne({"name": "a"}), DeleteOne({"_id": 1})])
        """
        if not isinstance(requests, (list, tuple)) or not requests:
            raise InvalidArgumentException(
                "bulk_write 需要非空的操作列表（pymongo 操作对象）")
        result = self._exec("bulk_write", list(requests))
        return self._finish({
            "inserted": getattr(result, "inserted_count", 0),
            "matched": getattr(result, "matched_count", 0),
            "modified": getattr(result, "modified_count", 0),
            "deleted": getattr(result, "deleted_count", 0),
            "upserted": getattr(result, "upserted_count", 0),
        })

    # ------------------------------------------------------------------ #
    # 索引
    # ------------------------------------------------------------------ #
    def ensure_index(self, fields: Any, unique: bool = False,
                     name: Optional[str] = None) -> "MongoCollection":
        """为查询字段建索引（幂等）。

        ``fields`` 可以是字段名（升序）、``(字段, 方向)`` 元组，
        或复合索引的元组列表::

            users.ensure_index("email", unique=True)
            users.ensure_index([("city", 1), ("age", -1)])

        **查询前先建索引**：无索引的 ``where`` 会退化成 COLLSCAN
        （全表扫描），集合越大越慢。
        """
        keys = self._index_keys(fields)
        kwargs: Dict[str, Any] = {"unique": bool(unique)}
        if name:
            kwargs["name"] = str(name)
        self._exec("create_index", keys, **kwargs)
        return self

    @staticmethod
    def _index_keys(fields: Any) -> Any:
        """把索引字段描述归一为 pymongo 的 keys 参数。"""
        if isinstance(fields, str):
            return [(fields, 1)]
        if isinstance(fields, tuple) and len(fields) == 2 and \
                isinstance(fields[0], str):
            return [(fields[0], 1 if fields[1] in (1, "asc", "ascending") else -1)]
        if isinstance(fields, (list, tuple)):
            keys = []
            for item in fields:
                if isinstance(item, str):
                    keys.append((item, 1))
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    keys.append((str(item[0]),
                                 1 if item[1] in (1, "asc", "ascending") else -1))
                else:
                    raise InvalidArgumentException(
                        f"非法索引字段描述: {item!r}")
            if not keys:
                raise InvalidArgumentException("索引字段不能为空")
            return keys
        raise InvalidArgumentException(
            f"非法索引字段描述: {fields!r}（字段名 / (字段, 方向) / 列表）")

    def drop_index(self, name: str) -> "MongoCollection":
        """删除索引。"""
        if not isinstance(name, str) or not name.strip():
            raise InvalidArgumentException(f"非法索引名: {name!r}")
        self._exec("drop_index", name.strip())
        return self

    def indexes(self) -> List[Dict[str, Any]]:
        """列出集合上的索引。"""
        return self._finish([dict(idx) for idx in self._exec("list_indexes")])

    # ------------------------------------------------------------------ #
    # Schema 校验
    # ------------------------------------------------------------------ #
    def schema(self, mapping: Dict[str, Any],
               required: Optional[Iterable[str]] = None
               ) -> "MongoCollection":
        """声明字段类型与必填项::

            users.schema({"name": str, "age": int, "tags": list},
                         required=["name"])

        校验语义：必填字段必须出现（值为 ``None`` 也算出现）；
        已声明字段做类型校验（float 宽容接受 int；bool 与 int/float 不兼容）；
        未声明字段放行（贴合 schemaless 精神）。
        """
        if not isinstance(mapping, dict):
            raise InvalidArgumentException("schema 需要 字段 -> 类型 字典")
        for key, typ in mapping.items():
            if typ not in (bool, int, float, str, list, dict):
                raise InvalidArgumentException(
                    f"字段 {key!r} 的类型须为 bool/int/float/str/list/dict，"
                    f"收到: {typ!r}")
            self._schema[str(key)] = typ
        if required:
            if isinstance(required, str):
                required = [required]
            for field in required:
                self._required.append(str(field))
        return self

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"MongoCollection({self.collection_name!r})"
