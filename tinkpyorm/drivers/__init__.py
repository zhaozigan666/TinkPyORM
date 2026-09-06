"""驱动注册表。

新增一种数据库的完整步骤::

    1. 新建 tinkpyorm/drivers/xxx.py，实现 Driver（继承 SQLDriver 或 NoSQLDriver）
    2. 用 @register_driver("xxx") 装饰器注册（或在 _LAZY_DRIVERS 登记惰性导入）
    3. 如需第三方依赖，在 pyproject.toml 的 optional-dependencies 中声明

上层（Connection / Builder / Query / Model）无需任何改动。
"""
from __future__ import annotations

from importlib import import_module
from typing import Dict, List, Optional, Type

from ..exceptions import DriverNotAvailable
from .base import Driver, NoSQLDriver, SQLDriver, UnsupportedOperation
from .sqlite import SQLiteDriver

__all__ = [
    "Driver", "SQLDriver", "NoSQLDriver", "SQLiteDriver",
    "UnsupportedOperation", "register_driver", "get_driver",
    "available_drivers",
]

#: 已注册的驱动（驱动名 -> 驱动类）
_DRIVERS: Dict[str, Type[Driver]] = {"sqlite": SQLiteDriver}

#: 惰性驱动的模块路径（依赖第三方包时才导入，缺依赖时不阻断 import）
_LAZY_DRIVERS: Dict[str, tuple] = {
    # "mysql": ("tinkpyorm.drivers.mysql", "MySQLDriver"),
    # "postgresql": ("tinkpyorm.drivers.postgresql", "PostgresDriver"),
}

#: 驱动名 -> 安装提示
_INSTALL_HINTS: Dict[str, str] = {
    "mysql": "pip install tinkpyorm[mysql]",
    "postgresql": "pip install tinkpyorm[postgres]",
    "mssql": "pip install tinkpyorm[mssql]",
    "mongodb": "pip install tinkpyorm[mongo]",
    "redis": "pip install tinkpyorm[redis]",
}


def register_driver(name: str, driver_cls: Optional[Type[Driver]] = None):
    """注册驱动。可作为装饰器使用::

        @register_driver("postgres")
        class PostgresDriver(SQLDriver): ...
    """
    def _register(cls: Type[Driver]) -> Type[Driver]:
        _DRIVERS[str(name).lower()] = cls
        return cls

    if driver_cls is not None:
        return _register(driver_cls)
    return _register


def get_driver(name: str) -> Type[Driver]:
    """取驱动类，未注册或依赖缺失时抛 ``DriverNotAvailable``。"""
    key = str(name or "").lower()
    if key in _DRIVERS:
        return _DRIVERS[key]

    if key in _LAZY_DRIVERS:
        module_path, class_name = _LAZY_DRIVERS[key]
        try:
            module = import_module(module_path)
        except ImportError as exc:
            hint = _INSTALL_HINTS.get(key, f"pip install tinkpyorm[{key}]")
            raise DriverNotAvailable(
                f"驱动 {key!r} 依赖未安装：{exc}。请执行：{hint}") from exc
        cls = getattr(module, class_name)
        _DRIVERS[key] = cls
        return cls

    raise DriverNotAvailable(
        f"未提供数据库驱动: {name!r}。已注册: {', '.join(available_drivers())}")


def available_drivers() -> List[str]:
    """当前可用的驱动名（含惰性驱动）。"""
    return sorted(set(_DRIVERS) | set(_LAZY_DRIVERS))
