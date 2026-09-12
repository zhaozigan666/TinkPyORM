"""文档集合 DocumentCollection（v0.8.0）—— schemaless 文档存储。

无需提前建表、无需声明字段，即可像操作文档一样读写 SQLite。存储为
"单表两列"：``_id INTEGER PRIMARY KEY AUTOINCREMENT`` + ``data TEXT``
（JSON 文档列），每个集合一张表，首次访问自动建表。

与 :class:`~tinkpyorm.query.Query` 的关系：本类是 Query 之上的**薄封装**，
条件、排序、分页全部复用既有 JSON 基建（``where_json`` 家族与
``update_json_*``），故操作符词汇、三重校验、缓存失效、事务与线程安全
行为与 Query 完全一致。注意与 :class:`~tinkpyorm.collection.Collection`
（查询**结果集**封装）是两个概念。

基本用法::

    from tinkpyorm import Db

    docs = Db.collection("users")
    uid = docs.insert({"name": "张三", "age": 25,
                       "profile": {"city": "北京"}, "tags": ["vip", "beta"]})

    docs.find(uid)                                  # 按文档 _id 取
    docs.where("profile.city", "=", "北京").select() # 点路径条件
    docs.where("age", ">=", 18).where("tags", "contains", "vip").select()
    docs.where("name", "张三").order("age", "desc").limit(10).select()

    docs.where("_id", uid).update_path({"age": 26})          # 路径级局部更新
    docs.where("_id", uid).patch({"profile": {"city": "上海"}})  # RFC 7396 合并
    docs.where("_id", uid).update({"name": "李四"})           # 整文档替换
    docs.where("age", "<", 18).delete()

    docs.promote("age")          # 字段提升：生成列（VIRTUAL），写路径零改动
    docs.ensure_index("age")     # 表达式索引（提升列上为普通索引）

字段名即 JSON 路径：``"name"`` / ``"profile.city"`` / ``"tags[0]"``（数组
索引用 ``[n]`` 写法）。``_id`` 恒为存储主键、文档内不可改；文档数据一律
存于 JSON 列，字段增减不需要任何 DDL。

实现要点：

- 终端方法执行后链式状态复位：同一实例可连续发起多个独立查询；
- 生成列对 ``PRAGMA table_info`` 隐藏（hidden=2/3），列清单必须用
  ``table_xinfo`` 读取；
- SQLite 会把建表 DDL 中的引号统一改写为反引号存储，提升列注册表
  （从 ``sqlite_master.sql`` 解析）须兼容反引号/双引号/无引号三种形态。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .builder import JSON_OPS
from .drivers import normalize_json_path
from .exceptions import InvalidArgumentException, QueryError
from .query import Query
from .utils import UNSET

_UNSET = UNSET

#: 合法集合名 / 列名 / 索引名（裸标识符）
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 提升列在 sqlite_master DDL 中的形态（驱动 quote_identifier 用反引号，
#: SQLite 存储时保留；兼容双引号/无引号，均为本类自己生成的格式）。
#: 引号必须左右配对（反向引用），否则 "age INTEGER GENERATED" 中的
#: 类型名 INTEGER 会被误捕获为列名。
_PROMOTED_RE = re.compile(
    r"([`\"'])(\w+)\1\s+(?:\w+\s+)?GENERATED\s+ALWAYS\s+AS\s+"
    r"\(\s*json_extract\(\s*([`\"'])data\3\s*,\s*'(\$[^']*)'\s*\)")

#: promote 允许的路径形态：至少一段对象键，不含数组索引
_PROMOTE_PATH_RE = re.compile(r"^\$(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")

#: schema 声明允许的 Python 类型
_VALID_TYPES = (bool, int, float, str, list, dict)

#: "字段缺失" 哨兵（区别于值恰为 None）
_MISSING = object()


class DocumentCollection:
    """文档集合：schemaless 文档存储的读写入口。

    实例由 :meth:`tinkpyorm.db.Db.collection` 创建；链式方法（where /
    order / limit / page）修改内部 Query 状态，终端方法（select / find /
    insert / update / delete / count 等）执行后状态自动复位。
    """

    def __init__(self, conn: Any, name: str, prefix: str = ""):
        if not isinstance(name, str) or not _IDENT_RE.match(name.strip()):
            raise InvalidArgumentException(
                f"非法集合名: {name!r}（须为字母/数字/下划线，且不以数字开头）")
        self.conn = conn
        self.name = name.strip()
        self.prefix = str(prefix or "")
        self.table = self.prefix + self.name
        self._schema: Dict[str, Any] = {}
        self._query: Optional[Query] = None
        self._ready = False
        self._columns_cache: Optional[List[str]] = None
        self._promoted_cache: Optional[Dict[str, str]] = None

    # ------------------------------------------------------------------ #
    # 内部：建表 / 列与提升列解析 / 链式状态
    # ------------------------------------------------------------------ #
    def _qi(self, ident: str) -> str:
        return self.conn.driver.quote_identifier(ident)

    def _ensure_table(self) -> None:
        """幂等建表（每实例仅执行一次）。"""
        if self._ready:
            return
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._qi(self.table)} ("
            f"{self._qi('_id')} INTEGER PRIMARY KEY AUTOINCREMENT, "
            f"{self._qi('data')} TEXT NOT NULL DEFAULT '{{}}')")
        self._ready = True

    def _q(self) -> Query:
        """当前链式 Query（懒建，首次访问自动建表）。"""
        if self._query is None:
            self._ensure_table()
            self._query = Query(self.conn, table=self.table)
        return self._query

    def _finish(self, result: Any) -> Any:
        """终端收尾：复位链式状态，同一实例可发起新的独立查询。"""
        self._query = None
        return result

    def _columns(self) -> List[str]:
        """全部列（含生成列）。VIRTUAL 生成列对 ``table_info`` 隐藏，
        必须用 ``table_xinfo``。"""
        if self._columns_cache is None:
            rows = self.conn.query(
                f"PRAGMA table_xinfo({self._qi(self.table)})")
            self._columns_cache = [r["name"] for r in rows]
        return self._columns_cache

    def _promoted(self) -> Dict[str, str]:
        """提升字段注册表 {JSON 路径: 列名}（从 sqlite_master DDL 解析）。

        不引入元数据表：提升列的 DDL 文本本身即注册表，跨进程/跨实例
        自洽。仅识别本类生成的形态，手工建的生成列若格式不同则不识别
        （按普通 JSON 路径处理，语义仍正确，仅不走列加速）。
        """
        if self._promoted_cache is None:
            rows = self.conn.query(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (self.table,))
            result: Dict[str, str] = {}
            if rows and rows[0].get("sql"):
                for m in _PROMOTED_RE.finditer(rows[0]["sql"]):
                    result[m.group(4)] = m.group(2)
            self._promoted_cache = result
        return self._promoted_cache

    def _resolve_field(self, field: str) -> Tuple[str, str]:
        """字段名 -> ('col', 列名) 或 ('path', 归一化 JSON 路径)。

        解析顺序：``_id`` 主键 -> 提升列（按路径精确匹配）-> JSON 路径。
        非法路径在 normalize_json_path 处统一报错。
        """
        if not isinstance(field, str) or not field.strip():
            raise InvalidArgumentException(f"非法字段名: {field!r}")
        f = field.strip()
        if f == "_id":
            return ("col", "_id")
        norm = normalize_json_path(f)
        promoted = self._promoted()
        if norm in promoted:
            return ("col", promoted[norm])
        return ("path", norm)

    # ------------------------------------------------------------------ #
    # 条件（复用 Query 操作符词汇）
    # ------------------------------------------------------------------ #
    def where(self, field: str, op: Any = _UNSET,
              value: Any = _UNSET) -> "DocumentCollection":
        """路径条件（AND 连接）。运算符与 Query.where 同一套词汇::

            docs.where("age", ">=", 18)
            docs.where("name", "张三")              # 省略运算符 = 等于
            docs.where("tags", ["vip", "beta"])     # 值为列表 = IN
            docs.where("deleted", None)             # 值为 null 或路径不存在
            docs.where("deleted", "exists")         # 路径存在（值为 null 亦算）
            docs.where("tags", "contains", "vip")   # 数组包含

        支持 ``where({'profile.city': '北京', 'age': 18})`` 字典形式。
        完整运算符见 ``tinkpyorm.JSON_OPS``。
        """
        return self._cond("AND", field, op, value)

    def where_or(self, field: str, op: Any = _UNSET,
                 value: Any = _UNSET) -> "DocumentCollection":
        """OR 连接的路径条件，签名同 :meth:`where`。"""
        return self._cond("OR", field, op, value)

    def where_length(self, path: str, op: Any,
                     value: Any = _UNSET) -> "DocumentCollection":
        """按数组 / 对象元素个数过滤::

            docs.where_length("tags", ">=", 3)
            docs.where_length("tags", 2)            # 等于 2
        """
        kind, ref = self._resolve_field(path)
        q = self._q()
        if kind == "col":
            if value is _UNSET:
                q._where("AND", (ref, op))
            else:
                q._where("AND", (ref, op, value))
        elif value is _UNSET:
            q.where_json_length("data", ref, op)
        else:
            q.where_json_length("data", ref, op, value)
        return self

    def _cond(self, logic: str, field: Any, op: Any, value: Any):
        q = self._q()
        if isinstance(field, dict):
            for k, v in field.items():
                self._cond(logic, k, _UNSET, v)
            return self
        # 2 参形式归一：第二参不是运算符时视为值（where('name', '张三')、
        # where('active', True)、where('age', [1, 2])）
        if op is not _UNSET and value is _UNSET and not (
                isinstance(op, str) and op.strip().lower() in JSON_OPS):
            op, value = _UNSET, op
        kind, ref = self._resolve_field(field)
        if kind == "col":
            if op is _UNSET:
                q._where(logic, (ref, value))
            else:
                q._where(logic, (ref, op, value))
            return self
        # JSON 路径条件
        if op is _UNSET:
            if isinstance(value, (list, tuple, set)) and \
                    not isinstance(value, (str, bytes)):
                op, value = "in", list(value)
            else:
                op = "="
        if op == "=" and value is _UNSET:
            raise QueryError(f"字段 {field!r} 的条件缺少值")
        if not (isinstance(op, str) and op.strip().lower() in JSON_OPS):
            raise QueryError(
                f"不支持的条件运算符: {op!r}。可选: " + ", ".join(sorted(JSON_OPS)))
        q._json_where(logic, "data", ref, op, value)
        return self

    # ------------------------------------------------------------------ #
    # 排序 / 分页（链式透传）
    # ------------------------------------------------------------------ #
    def order(self, field: str, direction: Optional[str] = None
              ) -> "DocumentCollection":
        """按路径排序：``order("age", "desc")``；提升列直接按列排。"""
        kind, ref = self._resolve_field(field)
        q = self._q()
        if kind == "col":
            q.order(ref, direction)
        else:
            q.order_json("data", ref, direction or "asc")
        return self

    def limit(self, length: int, offset: Optional[int] = None
              ) -> "DocumentCollection":
        self._q().limit(length, offset)
        return self

    def page(self, page: int, list_rows: int = 20) -> "DocumentCollection":
        self._q().page(page, list_rows)
        return self

    # ------------------------------------------------------------------ #
    # 读取终端
    # ------------------------------------------------------------------ #
    @staticmethod
    def _extract(doc: dict, path: str, default: Any = _MISSING) -> Any:
        """按点路径从文档取值；数组段用 ``[n]``。路径不存在返回 default。"""
        norm = normalize_json_path(path)
        cur: Any = doc
        for m in re.finditer(r'\.([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]', norm):
            key = m.group(1)
            if key is not None:
                if not isinstance(cur, dict) or key not in cur:
                    cur = _MISSING
                    break
                cur = cur[key]
            else:
                idx = int(m.group(2))
                if not isinstance(cur, list) or idx >= len(cur):
                    cur = _MISSING
                    break
                cur = cur[idx]
        if cur is _MISSING:
            return None if default is _MISSING else default
        return cur

    def _to_doc(self, row: dict) -> dict:
        """存储行 {'_id', 'data'} -> 文档 {'_id': ..., **data}。"""
        data = json.loads(row["data"]) if row["data"] else {}
        if not isinstance(data, dict):
            raise QueryError(
                f"集合 {self.table!r} 的 data 列含非对象 JSON（可能被外部"
                f"改写），文档列必须是 JSON 对象")
        data.pop("_id", None)  # 文档内 _id 以存储主键为准
        doc: Dict[str, Any] = {"_id": row["_id"]}
        doc.update(data)
        return doc

    def select(self):
        """查询文档列表，返回结果集 Collection（元素为文档 dict）。"""
        self._ensure_table()
        rows = self._q().select()
        from .collection import Collection
        return self._finish(Collection([self._to_doc(r) for r in rows]))

    def find(self, pk: Optional[Any] = None) -> Optional[dict]:
        """取单条文档。``find(id)`` 按文档 _id 查；无结果返回 None。"""
        self._ensure_table()
        q = self._q()
        if pk is not None:
            q.where("_id", pk)
        q.limit(1)
        rows = q.select()
        return self._finish(self._to_doc(rows[0]) if rows else None)

    def value(self, path: str, default: Any = None) -> Any:
        """取第一条文档中 ``path`` 的值。"""
        doc = self.find()
        if doc is None:
            return default
        return self._extract(doc, path, default)

    def column(self, path: str) -> List[Any]:
        """取所有文档中 ``path`` 的值列表（缺失为 None）。"""
        return [self._extract(d, path) for d in self.select()]

    def count(self) -> int:
        """满足当前条件的文档数。"""
        self._ensure_table()
        return self._finish(self._q().count())

    def distinct(self, path: str) -> List[Any]:
        """路径值去重列表（SQL DISTINCT，None 也算一个值）。"""
        self._ensure_table()
        kind, ref = self._resolve_field(path)
        q = self._q()
        if kind == "col":
            expr = self._qi(ref)
        else:
            expr = self.conn.driver.json_extract(self._qi("data"), ref)
        q.field_raw(f"{expr} AS v").distinct()
        return self._finish([r["v"] for r in q.select()])

    def group_counts(self, path: str) -> List[dict]:
        """按路径值分组计数，返回 ``[{'value': .., 'count': ..}, ..]``
        （按 count 降序）。"""
        self._ensure_table()
        kind, ref = self._resolve_field(path)
        q = self._q()
        if kind == "col":
            expr = self._qi(ref)
        else:
            expr = self.conn.driver.json_extract(self._qi("data"), ref)
        q.field_raw(f"{expr} AS v, COUNT(*) AS c").group("v").order("c", "desc")
        return self._finish(
            [{"value": r["v"], "count": r["c"]} for r in q.select()])

    # ------------------------------------------------------------------ #
    # 写入终端
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clean(doc: Any) -> dict:
        if not isinstance(doc, dict):
            raise QueryError(f"文档必须是 dict，收到: {type(doc).__name__}")
        return {k: v for k, v in doc.items() if k != "_id"}

    def insert(self, doc: dict) -> int:
        """插入文档，返回文档 _id。文档内的 ``_id`` 可显式指定主键。"""
        self._ensure_table()
        clean = self._clean(doc)
        self._validate(clean)
        payload: Dict[str, Any] = {"data": clean}
        if "_id" in doc:
            payload["_id"] = doc["_id"]
        return self._finish(self._q().insert(payload))

    def insert_all(self, docs: Sequence[dict]) -> int:
        """批量插入，返回条数。``_id`` 要么全部文档都带、要么都不带。"""
        self._ensure_table()
        docs = list(docs)
        if not docs:
            return self._finish(0)
        with_id = ["_id" in d for d in docs]
        if any(with_id) and not all(with_id):
            raise QueryError("insert_all 的 _id 必须全部带或全部不带")
        rows = []
        for d in docs:
            clean = self._clean(d)
            self._validate(clean)
            if all(with_id):
                rows.append({"_id": d["_id"], "data": clean})
            else:
                rows.append({"data": clean})
        self._q().insert_all(rows)
        return self._finish(len(docs))

    def update(self, doc: dict) -> int:
        """整文档替换（``_id`` 不可改，文档内的 ``_id`` 被忽略）。

        需先用 :meth:`where` 圈定范围；返回影响行数。
        """
        self._ensure_table()
        clean = self._clean(doc)
        self._validate(clean)
        return self._finish(self._q().update({"data": clean}))

    def update_path(self, mapping: Dict[str, Any],
                    ifnull: Any = None) -> int:
        """路径级局部更新（一次 SQL 原子写入，无读改写窗口）::

            docs.where("_id", 1).update_path({"age": 26, "profile.city": "上海"})

        ``ifnull`` 处理 data 列为 SQL NULL 的边界（本类建表默认 ``'{}'``，
        通常无需关心）。
        """
        self._ensure_table()
        if not isinstance(mapping, dict) or not mapping:
            raise QueryError("update_path 需要 {路径: 值} 字典且不能为空")
        normalized: Dict[str, Any] = {}
        for k, v in mapping.items():
            norm = normalize_json_path(k)
            if norm in self._schema:
                self._check_type(norm, v, self._schema[norm])
            normalized[norm] = v
        return self._finish(self._q().update_json(
            "data", normalized, ifnull={} if ifnull is None else ifnull))

    def patch(self, partial: dict) -> int:
        """RFC 7396 合并补丁：对象递归合并、数组整体替换、null 删除键::

            docs.where("_id", 1).patch({"profile": {"city": "上海"},
                                        "tmp": None})
        """
        self._ensure_table()
        if not isinstance(partial, dict) or not partial:
            raise QueryError("patch 需要补丁 dict 且不能为空")
        for k, v in partial.items():
            norm = normalize_json_path(k)
            if norm in self._schema and not isinstance(v, (dict, list)):
                self._check_type(norm, v, self._schema[norm])
        return self._finish(self._q().update_json_patch(
            "data", partial, ifnull={}))

    def delete(self, pk: Optional[Any] = None) -> int:
        """删除文档。``delete(id)`` 按 _id 删；否则按当前 where 条件删。"""
        self._ensure_table()
        q = self._q()
        if pk is not None:
            q.where("_id", pk)
        return self._finish(q.delete())

    # ------------------------------------------------------------------ #
    # 索引与字段提升
    # ------------------------------------------------------------------ #
    def ensure_index(self, path: str, unique: bool = False,
                     name: Optional[str] = None) -> "DocumentCollection":
        """为路径创建索引（幂等）。提升字段建普通列索引，其余字段建
        表达式索引（``json_extract`` 为确定性函数，SQLite 允许）::

            docs.ensure_index("age")
            docs.ensure_index("profile.city", name="idx_users_city")

        缺失路径的行在唯一索引下互不冲突（SQLite 视 NULL 为互异）。
        """
        self._ensure_table()
        kind, ref = self._resolve_field(path)
        if kind == "col":
            body = self._qi(ref)
        else:
            body = self.conn.driver.json_extract(self._qi("data"), ref)
        safe = re.sub(r"[^A-Za-z0-9_]", "_", ref).strip("_") or "doc"
        iname = name or f"idx_{self.table}_{safe}"
        if not _IDENT_RE.match(iname):
            raise InvalidArgumentException(f"非法索引名: {iname!r}")
        unique_sql = "UNIQUE " if unique else ""
        self.conn.execute(
            f"CREATE {unique_sql}INDEX IF NOT EXISTS {self._qi(iname)} "
            f"ON {self._qi(self.table)} ({body})")
        return self

    def promote(self, path: str, sql_type: Optional[str] = None,
                column: Optional[str] = None) -> "DocumentCollection":
        """把字段提升为 VIRTUAL 生成列（写路径零改动、值由引擎按需计算）::

            docs.promote("age")
            docs.promote("profile.city", sql_type="TEXT")

        提升后该字段条件自动改走列（可被 :meth:`ensure_index` 建普通索引），
        未提升字段不受影响。仅支持对象键路径（不含 ``[n]`` 数组索引）。
        ``sql_type`` 省略时不声明列类型（保持 JSON 原生类型比较语义）。
        """
        self._ensure_table()
        norm = normalize_json_path(path)
        if not _PROMOTE_PATH_RE.match(norm):
            raise InvalidArgumentException(
                f"promote 仅支持对象键路径（如 'age'、'profile.city'），"
                f"收到: {path!r}")
        col = column or norm[2:].replace(".", "_")
        if not _IDENT_RE.match(col) or col in ("_id", "data"):
            raise InvalidArgumentException(
                f"非法提升列名: {col!r}（可用 column 参数显式指定）")
        if col in self._columns():
            raise QueryError(f"列 {col!r} 已存在，无法重复提升")
        type_sql = f" {sql_type}" if sql_type else ""
        literal = "'" + norm.replace("'", "''") + "'"
        self.conn.execute(
            f"ALTER TABLE {self._qi(self.table)} ADD COLUMN "
            f"{self._qi(col)}{type_sql} GENERATED ALWAYS AS "
            f"(json_extract({self._qi('data')}, {literal})) VIRTUAL")
        self._columns_cache = None
        self._promoted_cache = None
        return self

    # ------------------------------------------------------------------ #
    # Schema 校验（可选，Db.set_config 的 collection_schema_mode 控制）
    # ------------------------------------------------------------------ #
    def schema(self, mapping: Dict[str, Any]) -> "DocumentCollection":
        """声明字段类型（可选）。仅在配置 ``collection_schema_mode`` 开启时
        于写入期校验；关闭时声明仅作文档::

            docs.schema({"age": int, "name": str, "tags": list})

        校验语义：只查"已声明路径上已出现的值"，不要求必填、不拒绝未声明
        字段（贴合 schemaless 精神）。支持类型：bool / int / float / str /
        list / dict（float 宽容接受 int；bool 与 int/float 互不兼容）。
        """
        if not isinstance(mapping, dict):
            raise InvalidArgumentException("schema 需要字段 -> 类型 字典")
        for k, t in mapping.items():
            norm = normalize_json_path(k)
            if t not in _VALID_TYPES:
                raise InvalidArgumentException(
                    f"字段 {k!r} 的类型须为 bool/int/float/str/list/dict，"
                    f"收到: {t!r}")
            self._schema[norm] = t
        return self

    def _check_type(self, path: str, value: Any, typ: Any) -> None:
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
                f"字段 {path!r} 的值类型不符：声明 {typ.__name__}，"
                f"实际 {type(value).__name__}")

    def _validate(self, doc: dict) -> None:
        if not self._schema:
            return
        if not getattr(self.conn.config, "collection_schema_mode", False):
            return
        for norm_path, typ in self._schema.items():
            v = self._extract(doc, norm_path, _MISSING)
            if v is _MISSING or v is None:
                continue
            self._check_type(norm_path, v, typ)

    # ------------------------------------------------------------------ #
    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"DocumentCollection({self.table!r})"
