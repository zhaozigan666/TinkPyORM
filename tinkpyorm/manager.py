"""数据库连接注册中心。

职责（对应重构方案 Phase 1）：

* **单一真相源**——消除 ``Db._connections`` 与 ``connection._default_connection``
  两套状态不同步的问题；
* **配置与连接分离**——``add()`` 只登记配置，首次取用时才建立连接（懒连接）；
* **生命周期**——同名重复注册自动关闭旧连接，提供 disconnect/reconnect/clear。

典型用法::

    from tinkpyorm import configure, connection

    configure({
        "default": {"database": "~/.app/vault.db", "prefix": "app_"},
        "log":     "sqlite:///~/.app/log.db",
    })
    connection().query("SELECT 1")
"""
from __future__ import annotations

import warnings
from typing import Any, Dict, Iterator, List, Mapping, Optional, Union, TYPE_CHECKING

from .config import Config, DEFAULT_ENV_PREFIX, env_dict
from .exceptions import ConfigError, ConnectionNotFound, InvalidArgumentException

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查
    from .connection import Connection

ConfigLike = Union[Config, Mapping[str, Any], str]


class DatabaseManager:
    """命名连接的注册中心（全局单例见模块底部 ``manager``）。

    参数::

        strict=False  # 未注册连接时：False=回退匿名内存库并告警（兼容旧行为）
                      #            True=抛 ConfigError
    """

    def __init__(self, strict: bool = False) -> None:
        self._configs: Dict[str, Config] = {}
        self._connections: Dict[str, "Connection"] = {}
        self._default: str = "default"
        self.strict = bool(strict)

    # ------------------------------------------------------------------ #
    # 注册
    # ------------------------------------------------------------------ #
    def add(self, name: str, config: ConfigLike) -> Config:
        """登记一个命名连接（不建立连接）。

        同名重复注册会先关闭并丢弃已建立的旧连接，避免句柄与文件锁泄漏。
        """
        cfg = self._as_config(config, name)
        if name in self._connections:
            self._close_quietly(self._connections.pop(name))
        self._configs[name] = cfg
        return cfg

    def configure(self, databases: Optional[Any] = None,
                  default: Optional[str] = None,
                  strict: Optional[bool] = None,
                  env_prefix: str = DEFAULT_ENV_PREFIX) -> None:
        """批量注册连接，并应用同名环境变量覆盖。

        ``databases`` 可以是::

            {"default": {...}, "log": "sqlite:///log.db"}   # 多连接
            "sqlite:///app.db"                              # 单个 DSN（注册为 default）
            Config(...)                                     # 单个配置对象
            None                                            # 仅从环境变量加载
        """
        if strict is not None:
            self.strict = bool(strict)

        items: Dict[str, ConfigLike] = {}
        if databases is None:
            pass
        elif isinstance(databases, str) or isinstance(databases, Config):
            items["default"] = databases
        elif isinstance(databases, Mapping):
            items = dict(databases)
        else:
            raise InvalidArgumentException(
                f"databases 必须是 dict / DSN 字符串 / Config，收到: {type(databases)}")

        for name, value in items.items():
            cfg = self._as_config(value, name)
            overrides = env_dict(env_prefix, name)
            if overrides:
                cfg = Config.merge(cfg, overrides)
            self.add(name, cfg)

        if default is not None:
            self.set_default(default)

    def set_default(self, name: str) -> None:
        """切换默认连接名（名称无需已注册，取用时才校验）。"""
        self._default = str(name)

    @property
    def default(self) -> str:
        return self._default

    def register_connection(self, name: str, conn: "Connection") -> "Connection":
        """直接登记一个已建立的连接（兼容 ``Db.set_config(Connection(...))``）。"""
        if name in self._connections and self._connections[name] is not conn:
            self._close_quietly(self._connections.pop(name))
        self._connections[name] = conn
        return conn

    # ------------------------------------------------------------------ #
    # 查询状态
    # ------------------------------------------------------------------ #
    def has(self, name: str) -> bool:
        """是否已注册该名称的配置。"""
        return name in self._configs

    def configured(self, name: Optional[str] = None) -> bool:
        """该名称（默认取 default）是否已有配置或已建立连接。"""
        key = name if name is not None else self._default
        return key in self._configs or key in self._connections

    def names(self) -> List[str]:
        """所有已注册的连接名（配置 + 连接）。"""
        return sorted(set(self._configs) | set(self._connections))

    def config(self, name: str) -> Config:
        """取已注册的配置，未注册抛 ``ConfigError``。"""
        try:
            return self._configs[name]
        except KeyError:
            raise ConnectionNotFound(
                f"未注册数据库连接: {name!r}，已注册: {', '.join(self.names()) or '(空)'}")

    def configs(self) -> Dict[str, Config]:
        return dict(self._configs)

    # ------------------------------------------------------------------ #
    # 连接获取与生命周期
    # ------------------------------------------------------------------ #
    def connection(self, name: Optional[str] = None) -> "Connection":
        """取连接（懒建立 + 缓存）。

        未注册时：``strict=True`` 抛 ``ConfigError``；否则回退到一个匿名的
        内存库并发出 ``DeprecationWarning``（兼容 v0.2.0 及更早的行为）。
        """
        from .connection import Connection

        key = name if name is not None else self._default
        conn = self._connections.get(key)
        if conn is not None:
            return conn

        cfg = self._configs.get(key)
        if cfg is None:
            # 显式指定名称却未注册：一律报错（与旧版 KeyError 行为一致）
            if self.strict or name is not None:
                raise ConnectionNotFound(
                    f"未注册数据库连接: {key!r}。"
                    f"请先调用 configure()/Db.set_config() 注册，"
                    f"已注册: {', '.join(self.names()) or '(空)'}")
            warnings.warn(
                f"数据库连接 {key!r} 未配置，已回退到匿名内存库（:memory:）。"
                f"该兼容行为将在后续版本移除，请显式调用 configure() 注册连接。",
                DeprecationWarning,
                stacklevel=3,
            )
            cfg = Config(name=key, type="sqlite", database=":memory:")

        conn = Connection.from_config(cfg)
        self._connections[key] = conn
        return conn

    def disconnect(self, name: Optional[str] = None) -> None:
        """关闭连接（保留配置）。``name=None`` 时关闭全部连接。"""
        if name is None:
            for conn in list(self._connections.values()):
                self._close_quietly(conn)
            self._connections.clear()
            return
        conn = self._connections.pop(name, None)
        if conn is not None:
            self._close_quietly(conn)

    def reconnect(self, name: Optional[str] = None) -> "Connection":
        """关闭并重建连接（配置不变）。"""
        key = name if name is not None else self._default
        self.disconnect(key)
        return self.connection(key)

    def clear(self) -> None:
        """关闭全部连接并清空所有配置（测试用）。"""
        self.disconnect()
        self._configs.clear()
        self._default = "default"

    def established(self) -> List[str]:
        """已实际建立连接的名称列表。"""
        return sorted(self._connections)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.configured(name)

    def __iter__(self) -> Iterator[str]:
        return iter(self.names())

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #
    @staticmethod
    def _as_config(config: ConfigLike, name: str) -> Config:
        if isinstance(config, Config):
            cfg = config
            return cfg if cfg.name == name else cfg.replace(name=name)
        if isinstance(config, str):
            return Config.from_dsn(config, name=name)
        if isinstance(config, Mapping):
            return Config.from_dict(config, name=name)
        raise InvalidArgumentException(
            f"连接 {name!r} 的配置类型不支持: {type(config)}，"
            f"可选 dict / DSN 字符串 / Config")

    @staticmethod
    def _close_quietly(conn: "Connection") -> None:
        try:
            conn.close()
        except Exception:  # pragma: no cover - 关闭失败不应影响切换
            pass


#: 全局注册中心（模块级单例）
manager = DatabaseManager()
