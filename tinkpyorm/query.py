"""查询构造器（对应 think-orm 的 BaseQuery/Query）。

核心设计复刻 think-orm：所有链式方法修改内部 ``options`` 字典，
终端方法将 options 交给 Builder 编译为 SQL 并执行。所有查询值
一律参数绑定，杜绝 SQL 注入。
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple, Union

from . import cache as _cache
from .builder import Builder, RawWhere
from .connection import Connection
from .exceptions import DataNotFound, InvalidArgumentException, QueryError
from .utils import Raw, raw, to_snake

# SQLite 单语句绑定变量上限（SQLITE_MAX_VARIABLE_NUMBER）
MAX_SQL_VARIABLES = 32766


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
        if not data:
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
        """单行后处理：JSON 解码 -> 获取器（attr）-> filter 回调。"""
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
