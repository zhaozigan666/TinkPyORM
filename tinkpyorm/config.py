"""数据库配置：值对象、DSN 解析与多来源加载。

设计目标（对应重构方案 Phase 1）：

* 配置与连接分离——``Config`` 只是值对象，注册时不建立连接；
* 单一来源——支持 dict / DSN / 环境变量 / 配置文件四种写法；
* 显式失败——未知数据库类型立即报错，不再静默降级为 SQLite；
* 驱动参数隔离——通用键之外的配置一律归入 ``options``，
  不再无差别透传给底层驱动（修复原 ``set_config`` 的 TypeError 问题）。
"""
from __future__ import annotations

import json
import os
from configparser import ConfigParser
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .exceptions import ConfigError, InvalidArgumentException

DEFAULT_TYPE = "sqlite"
DEFAULT_ENV_PREFIX = "TINKPYORM_"

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

#: 所有合法（已识别）的驱动名
KNOWN_TYPES = frozenset(TYPE_ALIASES.values())

#: 通用配置键。其余键一律归入驱动专属 options。
COMMON_KEYS = frozenset({
    "name", "type", "driver", "database", "dsn", "prefix",
    "host", "port", "user", "username", "password", "options",
    "sql_log_enabled", "sql_log_max", "connect_timeout", "path_expand",
})

#: 可直接透传给 sqlite3.connect() 的参数白名单
SQLITE_CONNECT_KEYS = frozenset({
    "timeout", "detect_types", "isolation_level", "check_same_thread",
    "factory", "cached_statements", "uri",
})

#: 布尔字段（用于环境变量的字符串转换）
_BOOL_FIELDS = frozenset({"sql_log_enabled", "path_expand"})
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
    """环境变量等字符串来源的类型转换（None 表示"未设置"，原样保留）。"""
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


def normalize_type(value: Optional[str]) -> str:
    """规范化并校验数据库类型，未知类型抛 ``ConfigError``。"""
    raw = str(value or DEFAULT_TYPE).strip().lower()
    name = TYPE_ALIASES.get(raw, raw)
    if name not in KNOWN_TYPES:
        raise ConfigError(
            f"未知的数据库类型: {value!r}。"
            f"可选类型: {', '.join(sorted(KNOWN_TYPES))}")
    return name


def parse_dsn(dsn: str) -> Dict[str, Any]:
    """解析 DSN 为配置字典。

    支持形式::

        sqlite:///abs/app.db          # 绝对路径（三斜杠 + 绝对路径按相对处理，见下）
        sqlite:////abs/app.db         # 绝对路径（四斜杠）
        sqlite:///./app.db            # 相对路径
        sqlite:///:memory:            # 内存库
        sqlite://relative/app.db      # 相对路径
        mysql://user:pwd@host:3306/db?charset=utf8mb4
        postgresql://user:pwd@host:5432/db
        mssql://user:pwd@host:1433/db
        redis://host:6379/0
        mongodb://host:27017/db
        app.db                        # 无 scheme，视为 SQLite 文件路径
    """
    if not isinstance(dsn, str):
        raise InvalidArgumentException(f"DSN 必须是字符串，收到: {type(dsn)}")
    text = dsn.strip()
    if "://" not in text:
        return {"type": "sqlite", "database": text}

    parts = urlparse(text)
    db_type = normalize_type(parts.scheme)
    out: Dict[str, Any] = {"type": db_type}

    if db_type != "sqlite":
        # SQLite 无主机概念：sqlite://rel/app.db 的 netloc 是路径首段
        if parts.hostname:
            out["host"] = unquote(parts.hostname)
        if parts.port:
            out["port"] = parts.port
        if parts.username:
            out["user"] = unquote(parts.username)
        if parts.password:
            out["password"] = unquote(parts.password)

    database = unquote(parts.path or "")
    if db_type == "sqlite":
        if parts.netloc and parts.netloc not in ("", ":"):
            database = parts.netloc + database
        # 四斜杠表示绝对路径：//abs/app.db -> /abs/app.db
        database = database[1:] if database.startswith("//") else database.lstrip("/")
        if database == "":
            database = ":memory:"
    else:
        database = database.lstrip("/")
    if database:
        out["database"] = database

    if parts.query:
        for key, values in parse_qs(parts.query, keep_blank_values=True).items():
            out[key] = values[0] if len(values) == 1 else values
    return out


@dataclass
class Config:
    """数据库连接配置（值对象，构造时不建立连接）。"""

    name: str = "default"
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

    def __post_init__(self) -> None:
        self.type = normalize_type(self.type)
        self.name = str(self.name or "default")
        self.prefix = str(self.prefix or "")
        if self.port is not None:
            self.port = int(self.port)
        if self.sql_log_max is not None:
            self.sql_log_max = int(self.sql_log_max)
        if self.connect_timeout is not None:
            self.connect_timeout = float(self.connect_timeout)
        self.sql_log_enabled = _to_bool(self.sql_log_enabled)
        self.path_expand = _to_bool(self.path_expand)
        self.options = dict(self.options or {})
        if self.path_expand:
            self.database = self._expand_path(self.database)

    # ------------------------------------------------------------------ #
    # 构造器
    # ------------------------------------------------------------------ #
    @classmethod
    def from_dict(cls, data: Mapping[str, Any], name: str = "default") -> "Config":
        """从字典构造。

        通用键之外的键一律归入 ``options``（不再透传给底层驱动）。
        支持 ``dsn`` 键：先解析 DSN，再用同字典中的其它键覆盖。
        """
        if not isinstance(data, Mapping):
            raise InvalidArgumentException(
                f"配置必须是字典/DSN 字符串/Config，收到: {type(data)}")

        merged: Dict[str, Any] = {}
        explicit_options: Dict[str, Any] = {}
        for key, value in data.items():
            if key == "options" and isinstance(value, Mapping):
                explicit_options.update(value)
                continue
            merged[key] = value

        if "dsn" in merged:
            merged = {**parse_dsn(str(merged.pop("dsn"))), **merged}

        kwargs: Dict[str, Any] = {"name": name}
        options: Dict[str, Any] = dict(explicit_options)
        for key, value in merged.items():
            if key == "options":
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

    @classmethod
    def from_dsn(cls, dsn: str, name: str = "default") -> "Config":
        return cls.from_dict(parse_dsn(dsn), name=name)

    @classmethod
    def from_env(cls, prefix: str = DEFAULT_ENV_PREFIX,
                 name: str = "default") -> Optional["Config"]:
        """从环境变量构造。无匹配变量时返回 ``None``。

        命名规则 ``{PREFIX}{NAME}_{FIELD}``，例如::

            TINKPYORM_DEFAULT_TYPE=sqlite
            TINKPYORM_DEFAULT_DATABASE=/var/lib/app/vault.db
            TINKPYORM_DEFAULT_OPTIONS_JOURNAL_MODE=WAL
            TINKPYORM_LOG_DATABASE=/var/log/app.db
        """
        data = env_dict(prefix, name)
        if not data:
            return None
        return cls.from_dict(data, name=name)

    @classmethod
    def merge(cls, base: "Config", overrides: Mapping[str, Any]) -> "Config":
        """在既有配置之上做字段级覆盖（options 为合并而非替换）。"""
        merged = base.to_dict()
        for key, value in overrides.items():
            if key == "options" and isinstance(value, Mapping):
                merged.setdefault("options", {}).update(value)
            else:
                merged[key] = value
        return cls.from_dict(merged, name=base.name)

    @classmethod
    def from_file(cls, path: str, name: str = "default") -> "Config":
        """从配置文件读取指定连接名。"""
        configs = load_file(path)
        if name not in configs:
            raise ConfigError(
                f"配置文件 {path} 中没有名为 {name!r} 的连接，"
                f"可选: {', '.join(sorted(configs))}")
        return configs[name]

    # ------------------------------------------------------------------ #
    # 输出与派生
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        data = {
            "name": self.name,
            "type": self.type,
            "database": self.database,
            "prefix": self.prefix,
            "sql_log_enabled": self.sql_log_enabled,
            "sql_log_max": self.sql_log_max,
            "connect_timeout": self.connect_timeout,
            "path_expand": self.path_expand,
        }
        for key in ("host", "port", "user", "password"):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        if self.options:
            data["options"] = dict(self.options)
        return data

    def replace(self, **overrides: Any) -> "Config":
        """返回应用覆盖项后的新配置（原对象不变）。"""
        return replace(self, **overrides)

    def dsn(self, hide_password: bool = True) -> str:
        """反解为 DSN 字符串（密码默认掩码，用于日志展示）。"""
        password = "***" if (hide_password and self.password) else self.password
        auth = ""
        if self.user:
            auth = self.user + (f":{password}" if password else "")
        host_part = ""
        if self.host:
            host_part = self.host + (f":{self.port}" if self.port else "")
        if auth or host_part:
            netloc = f"{auth}@{host_part}" if auth else host_part
        else:
            netloc = ""
        # 绝对路径 /abs/app.db -> sqlite:////abs/app.db（四斜杠），与 parse_dsn 对称
        return f"{self.type}://{netloc}/{self.database}"

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


def env_dict(prefix: str = DEFAULT_ENV_PREFIX, name: str = "default") -> Dict[str, Any]:
    """收集指定连接名的环境变量覆盖项（原始字符串值）。"""
    data: Dict[str, Any] = {}
    upper_name = name.upper()
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix):]
        if "_" not in rest:
            continue
        env_name, field_name = rest.split("_", 1)
        if env_name.upper() != upper_name:
            continue
        field_name = field_name.lower()
        if field_name.startswith("options_") and len(field_name) > 8:
            data.setdefault("options", {})[field_name[8:]] = value
        else:
            data[field_name] = value
    return data


def load_file(path: str) -> Dict[str, Config]:
    """从配置文件加载全部连接，返回 ``{连接名: Config}``。

    支持 JSON / TOML / INI，按扩展名判定。
    """
    file_path = Path(os.path.expanduser(str(path)))
    if not file_path.exists():
        raise ConfigError(f"配置文件不存在: {file_path}")
    suffix = file_path.suffix.lower()

    if suffix == ".json":
        with file_path.open("r", encoding="utf-8") as fp:
            raw = json.load(fp)
        if not isinstance(raw, dict):
            raise ConfigError(f"JSON 配置文件顶层必须是对象: {file_path}")
        return {name: Config.from_dict(section, name=name)
                for name, section in raw.items() if isinstance(section, dict)}

    if suffix in (".toml",):
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ModuleNotFoundError as exc:
                raise ConfigError(
                    "读取 TOML 配置需要 Python 3.11+（tomllib）或安装 tomli") from exc
        with file_path.open("rb") as fp:
            raw = tomllib.load(fp)
        return _sections_to_configs(raw, str(file_path))

    if suffix in (".ini", ".cfg", ".conf"):
        parser = ConfigParser()
        parser.read(file_path, encoding="utf-8")
        raw = {section: dict(parser.items(section)) for section in parser.sections()}
        return _sections_to_configs(raw, str(file_path))

    raise ConfigError(
        f"不支持的配置文件类型: {suffix or '(无扩展名)'}，可选 .json / .toml / .ini")


def _sections_to_configs(raw: Mapping[str, Any], path: str) -> Dict[str, Config]:
    configs: Dict[str, Config] = {}
    for name, section in raw.items():
        if not isinstance(section, Mapping):
            raise ConfigError(f"配置文件 {path} 中的 [{name}] 必须是键值对表")
        configs[name] = Config.from_dict(section, name=name)
    return configs
