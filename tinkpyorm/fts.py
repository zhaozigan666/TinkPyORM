"""SQLite FTS5 全文检索（v0.10.0）。

设计要点：

1. **不侵入 Builder** —— ``MATCH`` 的左值是"FTS 虚拟表名"而不是普通列，
   若塞进 ``_OPS`` 会让 ``where("name", "match", x)`` 对普通表也"看起来合法"。
   本模块复用 ``Query`` 已有的三个逃生舱（``where_raw`` / ``field_raw`` /
   ``order_raw``）拼装语句，``Builder`` 与 ``_OPS`` 零改动。
2. **两种同步模式**：
   - ``external``（默认）：FTS 表只存索引，原文留在主表；建表时自动挂
     INSERT/UPDATE/DELETE 三个触发器，主表改动即时同步，且能取原文做高亮。
   - ``standalone``：独立的 FTS 表（索引 + 原文同表），不依赖主表。
3. **默认分词器为 trigram** —— 中英文兼顾：英文按三字符子串、中文按三字符
   滑窗，均可做子串匹配。代价是索引约为原文 3–5 倍，且**查询串少于 3 个
   字符必然 0 命中**（trigram 的固有下限，调用方需知悉）。
4. **能力门槛** —— 驱动需声明 ``supports_fts``；SQLite 还需编译了 FTS5
   模块（运行时用 ``pragma_module_list`` 探测），否则在 ``create()`` 阶段
   就给出明确报错，而不是执行到一半失败。
5. **缓存联动** —— 触发器直接写 FTS 表，不经过 ``Query``，因此内容表的
   写操作不会自动失效"检索结果缓存"。本模块维护「内容表 → FTS 表」依赖
   注册表，``Query._invalidate_cache()`` 会顺带清理依赖表的缓存。

典型用法::

    docs = Db.fts("docs", columns={"title": 5.0, "body": 1.0})
    docs.create()                     # 虚拟表 + 三个触发器，幂等
    docs.search("sqlite OR 索引").rank().limit(10).select()
    docs.search("title:索引").highlight("body").select()
    docs.rebuild()                    # 批量导入后重建索引
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

from .drivers import UnsupportedOperation
from .exceptions import InvalidArgumentException, OrmError, QueryError
from .query import Query

__all__ = ["FtsIndex", "FtsQuery", "fts_quote", "DEFAULT_TOKENIZE"]

#: 默认分词器：trigram（中英文均可做子串匹配，查询串需 ≥3 字符）
DEFAULT_TOKENIZE = "trigram"

#: 合法标识符（表名 / 列名 / 索引名）
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: 合法 tokenize 声明：基础分词器 + 以空格分隔的可选参数
_TOKENIZE_RE = re.compile(r"^[A-Za-z0-9_]+(?: [A-Za-z0-9_]+)*$")

#: 「内容表 → FTS 表」依赖注册表，供写操作后失效检索引擎缓存
_DEPENDENTS: Dict[Tuple[str, str], set] = {}


# ---------------------------------------------------------------------- #
# 工具
# ---------------------------------------------------------------------- #
def fts_quote(text: Any) -> str:
    """把任意文本转成 FTS5 短语查询（内部双引号加倍转义）。

    用于把"用户输入的原始文本"安全地拼进查询串::

        docs.search(fts_quote("C++ 入门"))   # -> '"C++ 入门"'

    注意这不是 SQL 转义：检索词始终以绑定参数传入 SQL，本函数只处理
    FTS5 自身的查询语法。
    """
    return '"' + str(text).replace('"', '""') + '"'


def _register_dependency(database: str, content_table: str, fts_table: str) -> None:
    _DEPENDENTS.setdefault((database or "", content_table or ""), set()).add(fts_table)


def _unregister_dependency(database: str, content_table: str, fts_table: str) -> None:
    key = (database or "", content_table or "")
    tables = _DEPENDENTS.get(key)
    if not tables:
        return
    tables.discard(fts_table)
    if not tables:
        _DEPENDENTS.pop(key, None)


def invalidate_dependents(database: str, table: str) -> int:
    """内容表被写入后，顺带清掉其 FTS 索引的检索结果缓存。

    返回清除条数；无依赖时零成本（一次字典查找）。
    """
    tables = _DEPENDENTS.get(((database or ""), (table or "")))
    if not tables:
        return 0
    from . import cache as _cache
    cleared = 0
    for fts_table in tuple(tables):
        cleared += _cache.clear(_cache.prefix_for(database, fts_table))
    return cleared


# ---------------------------------------------------------------------- #
# 索引
# ---------------------------------------------------------------------- #
class FtsIndex:
    """FTS5 索引门面（对应 ``Db.fts(...)``）。

    参数说明：

    - ``name``：外模式下为**内容表名**，独立模式下为索引基名（实际虚拟表
      为 ``<name>_fts``）
    - ``columns``：``['title', 'body']`` 或 ``{'title': 5.0, 'body': 1.0}``；
      外模式省略时从内容表结构推断（排除主键），独立模式必填
    - ``tokenize``：分词器声明，默认 ``trigram``；也支持
      ``unicode61`` / ``unicode61 remove_diacritics 2`` 等
    - ``prefix``：前缀索引长度，如 ``(2, 3)`` → ``prefix='2 3'``
    - ``standalone``：``True`` 时建独立 FTS 表，不挂触发器、不依赖主表
    - ``content_rowid``：内容表的主键列，省略时自动探测（无 INTEGER 主键
      则退回 ``rowid``）
    - ``model``：绑定模型类，检索结果自动转成模型实例
    """

    def __init__(self, conn: Any, name: str, columns: Any = None, *,
                 tokenize: str = DEFAULT_TOKENIZE,
                 prefix: Optional[Sequence[int]] = None,
                 standalone: bool = False,
                 content_rowid: Optional[str] = None,
                 model: Optional[type] = None):
        if not isinstance(name, str) or not _IDENT_RE.match(name.strip()):
            raise InvalidArgumentException(
                f"非法索引名/表名: {name!r}（须为字母/数字/下划线，且不以数字开头）")
        driver = getattr(conn, "driver", None)
        if driver is not None and not getattr(driver, "supports_fts", False):
            raise UnsupportedOperation(
                f"{getattr(driver, 'name', '当前')} 驱动不支持全文检索："
                f"FTS5 是 SQLite 专有能力，其它驱动需自行实现方言钩子")
        self.conn = conn
        self.name = name.strip()
        self.fts_table = self.name + "_fts"
        self.model = model
        self.mode = "standalone" if standalone else "external"
        self.content_table: Optional[str] = None if standalone else self.name
        self.content_rowid = self._check_ident(content_rowid, "content_rowid") \
            if content_rowid else None
        self.tokenize = self._check_tokenize(tokenize)
        self.prefix = self._check_prefix(prefix)
        self._columns = self._normalize_columns(columns)
        self._inferred_rowid = False

    # ---- 基础工具 ---- #
    def _qi(self, ident: str) -> str:
        return self.conn.driver.quote_identifier(ident)

    @staticmethod
    def _sql_str(value: Any) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    @staticmethod
    def _check_ident(value: Any, what: str) -> str:
        if not isinstance(value, str) or not _IDENT_RE.match(value.strip()):
            raise InvalidArgumentException(
                f"非法{what}: {value!r}（须为字母/数字/下划线，且不以数字开头）")
        return value.strip()

    @staticmethod
    def _check_tokenize(tokenize: Any) -> str:
        text = str(tokenize or "").strip()
        if not text or not _TOKENIZE_RE.match(text):
            raise InvalidArgumentException(
                f"非法 tokenize 声明: {tokenize!r}。示例: 'trigram'、"
                f"'unicode61'、'unicode61 remove_diacritics 2'")
        return text

    @staticmethod
    def _check_prefix(prefix: Optional[Sequence[int]]) -> Tuple[int, ...]:
        if not prefix:
            return ()
        try:
            values = sorted({int(v) for v in prefix})
        except (TypeError, ValueError):
            raise InvalidArgumentException(f"prefix 必须是正整数序列: {prefix!r}")
        if not values or values[0] < 1:
            raise InvalidArgumentException(f"prefix 必须是正整数序列: {prefix!r}")
        return tuple(values)

    @staticmethod
    def _normalize_columns(columns: Any) -> Dict[str, float]:
        result: Dict[str, float] = {}
        if columns is None:
            return result
        if isinstance(columns, str):
            columns = [columns]
        if isinstance(columns, dict):
            items = columns.items()
        elif isinstance(columns, (list, tuple)):
            items = [(c, 1.0) for c in columns]
        else:
            raise InvalidArgumentException(
                f"columns 必须是 list/tuple/dict/str: {columns!r}")
        for name, weight in items:
            if not isinstance(name, str) or not _IDENT_RE.match(name.strip()):
                raise InvalidArgumentException(
                    f"非法列名: {name!r}（须为字母/数字/下划线，且不以数字开头）")
            try:
                result[name.strip()] = float(weight)
            except (TypeError, ValueError):
                raise InvalidArgumentException(f"列权重必须是数字: {name}={weight!r}")
        return result

    @property
    def columns(self) -> List[str]:
        """索引列名（按权重字典的插入顺序，bm25 权重即按此顺序传参）。"""
        return list(self._columns)

    def _weights(self) -> List[float]:
        return [self._columns[c] for c in self._columns]

    def _db(self) -> str:
        return getattr(getattr(self.conn, "config", None), "database", "") or ""

    # ---- 结构探测 ---- #
    def _table_info(self, table: str) -> List[dict]:
        rows = self.conn.query(f"PRAGMA table_info({self._qi(table)})")
        if not rows:
            raise QueryError(f"表不存在: {table}（全文检索需要已存在的内容表）")
        return rows

    def _ensure_columns(self) -> List[str]:
        """确定索引列；外模式下缺省从内容表推导主键与列清单。"""
        if self.mode == "external":
            info = self._table_info(self.content_table)
            if self.content_rowid is None:
                pk = next((r for r in info if r.get("pk")), None)
                if pk is not None and "INT" in str(pk.get("type", "")).upper():
                    self.content_rowid = pk["name"]
                else:
                    self.content_rowid = "rowid"
                    self._inferred_rowid = True
            if not self._columns:
                self._columns = {r["name"]: 1.0 for r in info
                                 if r["name"] != self.content_rowid}
            else:
                missing = [c for c in self._columns
                           if c not in {r["name"] for r in info}]
                if missing:
                    raise QueryError(
                        f"内容表 {self.content_table} 缺少索引列: "
                        + ", ".join(missing))
        elif not self._columns:
            raise InvalidArgumentException(
                "独立模式（standalone=True）必须显式提供 columns")
        return self.columns

    def _require_fts5(self) -> None:
        try:
            rows = self.conn.query(
                "SELECT name FROM pragma_module_list WHERE name = 'fts5'")
        except Exception:  # 老版本 SQLite 无 pragma_module_list 表值函数
            rows = [{"name": "fts5"}]
        if not rows:
            raise UnsupportedOperation(
                "当前 SQLite 未编译 FTS5 模块，无法创建全文索引"
                "（官方发行版默认包含；精简构建需重新编译）")

    def _require_index(self) -> None:
        rows = self.conn.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (self.fts_table,))
        if not rows:
            raise QueryError(
                f"FTS 索引不存在: {self.fts_table}，请先调用 create() 建索引")

    # ---- DDL ---- #
    def _create_sql(self, columns: Sequence[str]) -> str:
        parts = [self._qi(c) for c in columns]
        if self.mode == "external":
            parts.append(f"content={self._sql_str(self.content_table)}")
            parts.append(f"content_rowid={self._sql_str(self.content_rowid)}")
        parts.append(f"tokenize={self._sql_str(self.tokenize)}")
        if self.prefix:
            parts.append("prefix=" + self._sql_str(
                " ".join(str(p) for p in self.prefix)))
        return (f"CREATE VIRTUAL TABLE IF NOT EXISTS {self._qi(self.fts_table)} "
                f"USING fts5({', '.join(parts)})")

    def _trigger_sqls(self, columns: Sequence[str]) -> List[str]:
        """外模式同步触发器：插入/删除/更新三件套（幂等）。"""
        assert self.mode == "external"
        content = self._qi(self.content_table)
        fts = self._qi(self.fts_table)
        rowid = self._qi(self.content_rowid)
        col_sql = ", ".join(self._qi(c) for c in columns)
        new_vals = ", ".join(f"new.{self._qi(c)}" for c in columns)
        old_vals = ", ".join(f"old.{self._qi(c)}" for c in columns)
        ai = f"{self.fts_table}_ai"
        ad = f"{self.fts_table}_ad"
        au = f"{self.fts_table}_au"
        return [
            f"CREATE TRIGGER IF NOT EXISTS {self._qi(ai)} AFTER INSERT ON {content} "
            f"BEGIN INSERT INTO {fts}(rowid, {col_sql}) "
            f"VALUES (new.{rowid}, {new_vals}); END",
            f"CREATE TRIGGER IF NOT EXISTS {self._qi(ad)} AFTER DELETE ON {content} "
            f"BEGIN INSERT INTO {fts}({fts}, rowid, {col_sql}) "
            f"VALUES ('delete', old.{rowid}, {old_vals}); END",
            f"CREATE TRIGGER IF NOT EXISTS {self._qi(au)} AFTER UPDATE ON {content} "
            f"BEGIN INSERT INTO {fts}({fts}, rowid, {col_sql}) "
            f"VALUES ('delete', old.{rowid}, {old_vals}); "
            f"INSERT INTO {fts}(rowid, {col_sql}) "
            f"VALUES (new.{rowid}, {new_vals}); END",
        ]

    def _column_names(self) -> List[str]:
        return [r["name"] for r in self._table_info(self.fts_table)]

    # ---- 生命周期 ---- #
    def create(self, rebuild: bool = False) -> "FtsIndex":
        """创建虚拟表（外模式同时创建三个同步触发器），幂等。"""
        self._require_fts5()
        columns = self._ensure_columns()
        self.conn.execute(self._create_sql(columns))
        if self.mode == "external":
            for sql in self._trigger_sqls(columns):
                self.conn.execute(sql)
            _register_dependency(self._db(), self.content_table, self.fts_table)
        if rebuild:
            self.rebuild()
        return self

    def drop(self) -> None:
        """删除索引与（外模式下的）同步触发器，幂等。"""
        for suffix in ("_ai", "_ad", "_au"):
            self.conn.execute(
                f"DROP TRIGGER IF EXISTS {self._qi(self.fts_table + suffix)}")
        self.conn.execute(f"DROP TABLE IF EXISTS {self._qi(self.fts_table)}")
        if self.mode == "external":
            _unregister_dependency(self._db(), self.content_table, self.fts_table)

    def _command(self, command: str) -> None:
        fts = self._qi(self.fts_table)
        self.conn.execute(
            f"INSERT INTO {fts}({fts}) VALUES ({self._sql_str(command)})")

    def rebuild(self) -> "FtsIndex":
        """按内容表重建整个索引（批量导入后必做）。"""
        self._require_index()
        self._command("rebuild")
        return self

    def optimize(self) -> "FtsIndex":
        """合并索引段，减小体积（不影响检索结果）。"""
        self._require_index()
        self._command("optimize")
        return self

    def integrity_check(self) -> bool:
        """校验索引与内容表的一致性；不一致会抛 ``QueryError``。"""
        self._require_index()
        try:
            self._command("integrity-check")
        except Exception as exc:  # SQLite 用错误抛出校验失败
            raise QueryError(f"FTS 索引一致性校验失败: {exc}")
        return True

    # ---- 检索 ---- #
    def search(self, query: str) -> "FtsQuery":
        """以 FTS5 查询串发起检索（语法见 SQLite 文档，词项请用 fts_quote 包裹）。"""
        if not isinstance(query, str) or not query.strip():
            raise InvalidArgumentException("检索词不能为空")
        if self.mode == "external" and self.content_rowid is None:
            self._ensure_columns()   # 推理主键，供 rowid 映射
        self._require_index()
        return FtsQuery(self, query)

    def info(self) -> Dict[str, Any]:
        """索引概况（存在性 / 行数 / 列 / 分词器 / 同步模式）。"""
        exists = bool(self.conn.query(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            (self.fts_table,)))
        rows = 0
        if exists:
            rows = self.conn.query(
                f"SELECT count(*) AS c FROM {self._qi(self.fts_table)}")[0]["c"]
        return {
            "table": self.name,
            "fts_table": self.fts_table,
            "mode": self.mode,
            "columns": dict(self._columns),
            "tokenize": self.tokenize,
            "prefix": list(self.prefix),
            "exists": exists,
            "rows": rows,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (f"<FtsIndex {self.fts_table} mode={self.mode} "
                f"tokenize={self.tokenize}>")


# ---------------------------------------------------------------------- #
# 检索
# ---------------------------------------------------------------------- #
class FtsQuery:
    """FTS5 检索链（由 :meth:`FtsIndex.search` 构造）。

    与 ``Query`` 的分工：``FtsQuery`` 只负责"检索 + 排序 + 高亮"，
    主表字段过滤通过 ``where()`` 转成 ``rowid IN (SELECT ...)`` 子查询，
    其余能力（分页、缓存、模型绑定）复用 ``Query``。
    """

    def __init__(self, index: FtsIndex, match: str):
        self.index = index
        self.match = match
        self._filters: List[Tuple[tuple, dict]] = []
        self._highlight: Optional[Tuple[str, str, str]] = None
        self._snippet: Optional[Tuple[str, str, str, str, int]] = None
        self._rank_reverse = False
        self._order: Optional[Tuple[str, Optional[str]]] = None
        self._limit: Optional[int] = None
        self._offset: Optional[int] = None
        self._cache: Optional[float] = None

    # ---- 链式 ---- #
    def where(self, *args: Any, **kwargs: Any) -> "FtsQuery":
        """叠加内容表条件（仅外模式）；多条之间为 AND。"""
        if self.index.mode != "external":
            raise QueryError("独立模式（standalone）没有内容表，无法叠加 where 条件")
        self._filters.append((args, kwargs))
        return self

    def rank(self, reverse: bool = False) -> "FtsQuery":
        """按相关度排序（bm25，越小越相关）；``reverse=True`` 取最不相关。"""
        self._rank_reverse = bool(reverse)
        self._order = None
        return self

    def order(self, field: str, direction: Optional[str] = None) -> "FtsQuery":
        """按字段排序（覆盖相关度排序）。"""
        FtsIndex._check_ident(field, "排序字段")
        self._order = (field, direction)
        return self

    def limit(self, length: int, offset: Optional[int] = None) -> "FtsQuery":
        self._limit = int(length)
        self._offset = offset
        return self

    def page(self, page: int, list_rows: int = 20) -> "FtsQuery":
        current = max(1, int(page))
        self._limit = int(list_rows)
        self._offset = (current - 1) * int(list_rows)
        return self

    def cache(self, expire: Union[int, float] = 60) -> "FtsQuery":
        """缓存检索结果；内容表写入后依赖注册表会自动失效它。"""
        self._cache = float(expire)
        return self

    def highlight(self, column: str, open: str = "<em>",
                  close: str = "</em>") -> "FtsQuery":
        """返回高亮列到 ``_highlight``（标记原样作为 SQL 字面量，已转义）。"""
        self.index._ensure_columns()
        self._highlight = (column, str(open), str(close))
        return self

    def snippet(self, column: str, open: str = "<em>", close: str = "</em>",
                ellipsis: str = "...", tokens: int = 8) -> "FtsQuery":
        """返回摘要列到 ``_snippet``（``tokens`` 为摘要窗口内的词数）。"""
        self.index._ensure_columns()
        self._snippet = (column, str(open), str(close), str(ellipsis), int(tokens))
        return self

    # ---- 编译 ---- #
    def _col_index(self, column: str) -> int:
        columns = self.index.columns
        if column not in columns:
            raise InvalidArgumentException(
                f"列 {column!r} 不在索引内，可选: " + ", ".join(columns))
        return columns.index(column)

    def _bm25(self) -> str:
        qi = self.index._qi
        weights = self.index._weights()
        args = "".join(f", {w!r}" for w in weights)
        return f"bm25({qi(self.index.fts_table)}{args})"

    def _build(self) -> Query:
        idx = self.index
        qi = idx._qi
        fts = qi(idx.fts_table)
        q = Query(idx.conn, table=idx.fts_table, model=idx.model)

        # 1) 检索条件：MATCH 左值为虚拟表名，右值绑定参数
        q.where_raw(f"{fts} MATCH ?", [self.match])

        # 2) 内容表条件 -> rowid IN (子查询)
        for args, kwargs in self._filters:
            sub = Query(idx.conn, table=idx.content_table)
            sub.field(idx.content_rowid)
            sub.where(*args, **kwargs)
            sub_sql, sub_params = sub.builder.select(sub.options)
            q.where_raw(f"{fts}.rowid IN ({sub_sql})", sub_params)

        # 3) 输出列：rowid 映射成主键名 + 原列 + 相关度 + 可选高亮/摘要
        rowid = idx.content_rowid or "rowid"
        fields = [f"{fts}.rowid AS {qi(rowid)}", "*",
                  f'{self._bm25()} AS "_score"']
        if self._highlight is not None:
            col, open_mark, close_mark = self._highlight
            fields.append(
                f'highlight({fts}, {self._col_index(col)}, '
                f'{idx._sql_str(open_mark)}, {idx._sql_str(close_mark)}) '
                f'AS "_highlight"')
        if self._snippet is not None:
            col, open_mark, close_mark, ellipsis, tokens = self._snippet
            fields.append(
                f'snippet({fts}, {self._col_index(col)}, '
                f'{idx._sql_str(open_mark)}, {idx._sql_str(close_mark)}, '
                f'{idx._sql_str(ellipsis)}, {tokens}) AS "_snippet"')
        q.field_raw(", ".join(fields))

        # 4) 排序
        if self._order is not None:
            q.order(*self._order)
        else:
            q.order_raw(self._bm25() + (" DESC" if self._rank_reverse else " ASC"))

        # 5) 分页与缓存
        if self._limit is not None:
            q.limit(self._limit, self._offset)
        if self._cache is not None:
            q.cache(self._cache)
        return q

    # ---- 终端 ---- #
    def select(self) -> Any:
        """执行检索，返回 ``Collection``（绑定模型时为模型实例集合）。"""
        try:
            return self._build().select()
        except OrmError:
            raise
        except Exception as exc:
            raise QueryError(f"FTS5 检索失败: {exc}")

    def first(self) -> Any:
        """取第一条结果（无结果返回 None）。"""
        rows = self.limit(1)._build().select()
        return rows[0] if len(rows) else None

    def count(self) -> int:
        """满足检索条件的记录数（不受分页影响）。"""
        try:
            return self._build().limit(None).count()
        except OrmError:
            raise
        except Exception as exc:
            raise QueryError(f"FTS5 计数失败: {exc}")

    def to_sql(self) -> str:
        """返回将要执行的 SQL（参数已内联，仅调试用）。"""
        return self._build().fetch_sql().select()

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<FtsQuery {self.match!r} on {self.index.fts_table}>"
