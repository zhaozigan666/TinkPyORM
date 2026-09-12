"""内部配置载体（不对外导出，属驱动抽象层的实现细节）。

v0.3.1 起撤销"集中式配置"（Config/DatabaseManager/DSN/环境变量/配置文件
等对外机制全部移除，配置入口收敛回 ``Db.set_config``）。本模块只保留
驱动实例化所需的极简值对象 :class:`Config` 与类型校验逻辑。

字段语义:
    type        驱动名（sqlite/mysql/postgresql/...），未注册的类型立即报错。
                合法性以**活驱动注册表**为准（内置名 + ``register_driver()``
                运行时注册的第三方驱动），故自定义驱动可直接用其名配置；
    database    库文件路径 / 库名；
    prefix      表前缀（由 ``Db.set_config`` 消费）；
    options     驱动专属参数（如 SQLite 的 journal_mode / timeout），
                通用键之外的配置一律归入此处，不再无差别透传底层驱动。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from .exceptions import InvalidArgumentException

DEFAULT_TYPE = "sqlite"

#: type 别名 -> 规范驱动名
TYPE_ALIASES: Dict[str, str] = {
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
    "mysql": "mysql",
    "mariadb": "mysql",
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "pgsql": "postgresql",
    "pg": "postgresql",
    "mssql": "mssql",
    "sqlserver": "mssql",
    "sql_server": "mssql",
    "mongodb": "mongodb",
    "mongo": "mongodb",
    "redis": "redis",
}

#: 内置（保留）驱动名 —— 别名映射的目标集合。这些名字无需驱动实现即可在
#: 配置阶段通过校验（MySQL / PostgreSQL 等属"预留名"，实到连接时才会因缺少
#: 驱动实现而抛 ``DriverNotAvailable``）。第三方驱动的合法性不在此表内，
#: 而由活注册表判定，见 :func:`_registered_types` 与 :func:`available_type_names`。
KNOWN_TYPES = frozenset(TYPE_ALIASES.values())

#: 通用配置键。其余键一律归入驱动专属 options。
COMMON_KEYS = frozenset({
    "name", "type", "driver", "database", "dsn", "prefix",
    "host", "port", "user", "username", "password", "options",
    "sql_log_enabled", "sql_log_max", "connect_timeout", "path_expand",
    "collection_schema_mode",
})

#: 可直接透传给 sqlite3.connect() 的参数白名单
SQLITE_CONNECT_KEYS = frozenset({
    "timeout", "detect_types", "isolation_level", "check_same_thread",
    "factory", "cached_statements", "uri",
})

#: 布尔字段（from_dict 的字符串转换）
_BOOL_FIELDS = frozenset({"sql_log_enabled", "path_expand",
                          "collection_schema_mode"})
_INT_FIELDS = frozenset({"port", "sql_log_max"})
_FLOAT_FIELDS = frozenset({"connect_timeout"})

_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y", "t"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "n", "f"})


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE_VALUES:
            return True
        if v in _FALSE_VALUES:
            return False
    return bool(value)


def _coerce(field_name: str, value: Any) -> Any:
    """字符串来源的类型转换（None 表示"未设置"，原样保留）。"""
    if value is None:
        return None
    if field_name in _BOOL_FIELDS:
        return _to_bool(value)
    if field_name == "sql_log_max":
        if isinstance(value, str) and value.strip().lower() in ("", "none", "null"):
            return None
        return int(value)
    if field_name in _INT_FIELDS:
        return int(value)
    if field_name in _FLOAT_FIELDS:
        return float(value)
    return value


def _registered_types() -> frozenset:
    """取驱动注册表中当前可用的驱动名（含运行时注册的第三方驱动）。

    惰性导入是**必需**而非优化：``drivers/base.py`` 反向依赖本模块
    （``from ..config import Config``），模块级导入会造成循环导入。

    驱动层抛错时退化为空集合：驱动层故障不应连带让配置构造失败，
    校验随即回落到 :data:`KNOWN_TYPES`（行为等同修复前）。
    """
    try:
        from .drivers import available_drivers
    except Exception:  # pragma: no cover - 驱动层异常不应阻断配置
        return frozenset()
    return frozenset(available_drivers())


def available_type_names() -> frozenset:
    """当前可用的数据库类型名（内置保留名 ∪ 已注册驱动名）。"""
    return KNOWN_TYPES | _registered_types()


def normalize_type(value: Optional[str]) -> str:
    """规范化并校验数据库类型，未注册的类型抛异常。

    校验顺序：先查内置保留名（快路径，零导入），未命中再查活驱动注册表。
    后者使 ``register_driver("oracle", OracleDriver)`` 之后
    ``{"type": "oracle"}`` 立即成为合法配置。
    """
    raw = str(value or DEFAULT_TYPE).strip().lower()
    name = TYPE_ALIASES.get(raw, raw)
    if name in KNOWN_TYPES or name in _registered_types():
        return name
    raise InvalidArgumentException(
        f"未知的数据库类型: {value!r}。"
        f"可选类型: {', '.join(sorted(available_type_names()))}。"
        f"自定义驱动需先注册: register_driver({raw!r}, YourDriver)")


@dataclass
class Config:
    """数据库连接配置（值对象，构造时不建立连接）。"""

    type: str = DEFAULT_TYPE
    database: str = ":memory:"
    host: Optional[str] = None
    port: Optional[int] = None
    user: Optional[str] = None
    password: Optional[str] = None
    prefix: str = ""
    #: 驱动专属参数（如 SQLite 的 timeout/journal_mode，MySQL 的 charset）
    options: Dict[str, Any] = field(default_factory=dict)
    sql_log_enabled: bool = True
    sql_log_max: Optional[int] = 1000
    connect_timeout: Optional[float] = None
    path_expand: bool = True
    #: 文档集合（DocumentCollection）写入期 schema 校验开关（v0.8.0）。
    #: 开启时对已声明路径上的值做类型校验；关闭时 schema 声明仅作文档。
    collection_schema_mode: bool = False

    def __post_init__(self) -> None:
        self.type = normalize_type(self.type)
        self.prefix = str(self.prefix or "")
        if self.port is not None:
            self.port = int(self.port)
        if self.sql_log_max is not None:
            self.sql_log_max = int(self.sql_log_max)
        if self.connect_timeout is not None:
            self.connect_timeout = float(self.connect_timeout)
        self.sql_log_enabled = _to_bool(self.sql_log_enabled)
        self.path_expand = _to_bool(self.path_expand)
        self.collection_schema_mode = _to_bool(self.collection_schema_mode)
        self.options = dict(self.options or {})
        if self.path_expand:
            self.database = self._expand_path(self.database)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Config":
        """从字典构造。

        通用键之外的键一律归入 ``options``（不再透传给底层驱动）。
        支持 ``driver``/``dsn`` 作为 ``type`` 的别名（``dsn`` 仅接受裸路径）。
        """
        if not isinstance(data, Mapping):
            raise InvalidArgumentException(
                f"配置必须是字典/连接对象/路径字符串，收到: {type(data)}")

        kwargs: Dict[str, Any] = {}
        options: Dict[str, Any] = {}
        for key, value in data.items():
            if key == "options" and isinstance(value, Mapping):
                options.update(value)
                continue
            if key == "dsn":
                # 裸路径 DSN（无 scheme 视为 SQLite 文件），历史兼容写法
                kwargs.setdefault("database", value)
                continue
            if key in ("username",):
                kwargs["user"] = value
            elif key in ("driver",):
                kwargs["type"] = value
            elif key in COMMON_KEYS:
                kwargs[key] = _coerce(key, value)
            else:
                options[key] = value
        if options:
            kwargs["options"] = options
        return cls(**kwargs)

    # ------------------------------------------------------------------ #
    # 输出
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        data = {
            "type": self.type,
            "database": self.database,
            "prefix": self.prefix,
            "sql_log_enabled": self.sql_log_enabled,
            "sql_log_max": self.sql_log_max,
            "connect_timeout": self.connect_timeout,
            "path_expand": self.path_expand,
            "collection_schema_mode": self.collection_schema_mode,
        }
        for key in ("host", "port", "user", "password"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.options:
            data["options"] = dict(self.options)
        return data

    # ------------------------------------------------------------------ #
    # 内部工具
    # ------------------------------------------------------------------ #
    @staticmethod
    def _expand_path(database: Any) -> Any:
        """展开 ``~`` 与环境变量（仅本地文件路径，不影响 :memory: 与网络库）。"""
        if not isinstance(database, str) or not database:
            return database
        if database == ":memory:" or "://" in database:
            return database
        expanded = os.path.expandvars(os.path.expanduser(database))
        # 统一路径分隔符（Windows 上 ~ 展开会混入反斜杠）
        return os.path.normpath(expanded) if os.path.isabs(expanded) else expanded

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Config {self.type}:{self.database}>"
