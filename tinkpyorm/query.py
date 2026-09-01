"""查询构造器（对应 think-orm 的 BaseQuery/Query）。

核心设计复刻 think-orm：所有链式方法修改内部 ``options`` 字典，
终端方法将 options 交给 Builder 编译为 SQL 并执行。所有查询值
一律参数绑定，杜绝 SQL 注入。
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from .builder import Builder, RawWhere
from .connection import Connection
from .exceptions import DataNotFound, QueryError
from .utils import Raw, raw, quote_ident, to_snake

CacheValue = Tuple[float, Any]


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
            "json": [],         # JSON 字段（查询时自动解码）
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
        self._cache_store: Dict[str, CacheValue] = {}
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
        return self.table(self.prefix + table)

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
        return self._where("AND", Raw(f"{quote_ident(field1)} {op.upper()} {quote_ident(field2)}"))

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
    def json(self, fields: Sequence[str]) -> "Query":
        """指定查询结果自动 JSON 解码的字段。"""
        self.options["json"] = list(fields) if isinstance(fields, (list, tuple)) else [fields]
        return self

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

    def cache(self, key: Union[bool, str] = True, expire: int = 60) -> "Query":
        """查询结果缓存（进程内内存缓存，标准库实现）。"""
        self.options["cache"] = (key, expire)
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
        q._cache_store = self._cache_store
        return q

    # ------------------------------------------------------------------ #
    # 终端方法：读取
    # ------------------------------------------------------------------ #
    def _resolve_conn(self) -> Connection:
        if self.conn is None:
            from .connection import get_default_connection
            self.conn = get_default_connection()
            self.builder = Builder(self.conn)
        return self.conn

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

        cache_key, expire = self._cache_key()
        if cache_key and cache_key in self._cache_store:
            ts, rows = self._cache_store[cache_key]
            if time.time() - ts < expire:
                return self._build_result(rows, sql=False)
            del self._cache_store[cache_key]

        rows = self._resolve_conn().query(sql, params)
        if cache_key:
            self._cache_store[cache_key] = (time.time(), rows)
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

        rows = self._resolve_conn().query(sql, params)
        if not rows:
            if self.options["fail_exception"]:
                raise DataNotFound("查询无结果")
            if self.options["allow_empty"]:
                if self.model is not None:
                    return self.model._from_data({})
                return {}
            return None
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
        if field == "*":
            expr = "COUNT(*)"
        else:
            expr = f"{fn}({quote_ident(field)})"
        q = self.copy()
        q.options["field"] = Raw(expr)
        q.options["limit"] = None
        sql, params = q._execute_select()
        if q.options["fetch_sql"]:
            return conn_render(sql, params)
        row = q._resolve_conn().query(sql, params)
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
        return conn.insert(sql, params)

    def insert_all(self, data_list: List[dict]) -> int:
        """批量插入，返回插入行数。"""
        if not data_list:
            return 0
        conn = self._resolve_conn()
        sql, params = self.builder.insert_all(self.options, data_list)
        self._last_sql, self._last_params = sql, params
        if self.options["fetch_sql"]:
            return conn_render(sql, params)
        conn.execute(sql, params)
        return len(data_list)

    def update(self, data: Optional[dict] = None) -> int:
        """更新数据，返回影响行数。必须存在 where 条件。"""
        if data is None:
            data = self.options.get("data") or {}
        if not data:
            raise QueryError("update 数据不能为空")
        self._apply_soft_delete()
        conn = self._resolve_conn()
        sql, params = self.builder.update(self.options, data)
        self._last_sql, self._last_params = sql, params
        if self.options["fetch_sql"]:
            return conn_render(sql, params)
        return conn.execute(sql, params)

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
        return conn.execute(sql, params)

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
        return self.update({field: raw(f"{quote_ident(field)} + {int(step)}")})

    def dec(self, field: str, step: int = 1) -> int:
        """字段自减。"""
        return self.update({field: raw(f"{quote_ident(field)} - {int(step)}")})

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

    def _cache_key(self) -> Tuple[Optional[str], int]:
        cache = self.options.get("cache")
        if not cache:
            return None, 0
        key, expire = cache
        if key is True or key is None:
            sql, params = self._execute_select()
            key = f"tinkpyorm:{sql}:{params}"
        return key, expire or 60

    def _build_result(self, rows: List[dict], sql: bool = True) -> Any:
        """原始行 -> 应用 json/attr/filter -> (模型实例) 结果集。"""
        from .collection import Collection

        results = []
        for row in rows:
            item = dict(row)
            for f in self.options["json"]:
                if f in item and isinstance(item[f], str):
                    import json
                    try:
                        item[f] = json.loads(item[f])
                    except (ValueError, TypeError):
                        pass
            for f, cb in self.options["attr"].items():
                if f in item:
                    item[f] = cb(item[f])
            for cb in self.options["filter"]:
                item = cb(item)
            results.append(item)

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
