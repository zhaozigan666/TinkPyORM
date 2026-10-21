"""数据库产品版本解析与校验（v0.11.0）。

版本号在配置层以 ``version`` 传入，语义随数据库产品而定：

* **MySQL**：``5.7`` / ``5.7.44`` / ``8.0`` / ``8.0.36`` / ``8.4`` / ``9.1``；
  缺省 ``5.7.0``（按 5.7 处理）。
* **SQLite**：不读取 ``version``（版本由运行时 ``sqlite3`` 决定）。

解析规则（与产品无关的通用部分）：

1. 接受 ``str`` / ``int`` / ``float`` / 2~3 元组 / :class:`Version`；
2. 缺省段补 0：``"8"`` → ``8.0.0``，``"8.0"`` → ``8.0.0``；
3. 允许 ``-`` 后缀（发行版标记），只取前三段数字：
   ``"8.0.36-0ubuntu0.22.04.1"`` → ``8.0.36``；
4. 拒绝空串、非数字段、超过 3 段且无法截断的形式。

MySQL 支持范围校验（见 :data:`MYSQL_SUPPORTED`）：

* ``5.7.x`` —— 最低支持线；
* ``8.0.x`` ~ ``8.4.x`` —— 8.x 主线（8.4 为 LTS）；
* ``9.x`` —— 9 主线（9.0 起移除 ``mysql_native_password``）。

超出范围（如 5.6 / 6.x / 7.x / 8.5）直接抛 ``InvalidArgumentException``，
而不是"按最近的版本猜"——猜错版本会让版本门禁静默放行或静默拦截。
"""
from __future__ import annotations

import re
from typing import Any, Optional, Tuple

from .exceptions import InvalidArgumentException

__all__ = [
    "Version", "parse_version", "DEFAULT_MYSQL_VERSION", "MYSQL_SUPPORTED",
]

#: MySQL 缺省版本（配置未给 ``version`` 时按 5.7 处理）
DEFAULT_MYSQL_VERSION = "5.7.0"

#: 支持范围的人类可读描述（错误信息与文档共用）
MYSQL_SUPPORTED = "5.7.x / 8.0.x–8.4.x / 9.x"

#: MongoDB 缺省版本（配置未给 ``version`` 时按 4.0 处理）
DEFAULT_MONGO_VERSION = "4.0.0"

#: MongoDB 支持范围（v0.12.0）。4.0 是 pymongo 4.11+ 支持的服务端下限。
MONGO_SUPPORTED = "4.0.x / 4.2.x / 4.4.x / 5.0.x / 6.0.x / 7.0.x / 8.x"

_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:[-+~].*)?$")


class Version(tuple):
    """三段式版本号（``major.minor.patch``），可直接与元组比较。"""

    __slots__ = ()

    def __new__(cls, major: Any, minor: Any = 0, patch: Any = 0) -> "Version":
        return tuple.__new__(cls, (int(major), int(minor), int(patch)))

    @property
    def major(self) -> int:
        return self[0]

    @property
    def minor(self) -> int:
        return self[1]

    @property
    def patch(self) -> int:
        return self[2]

    def __str__(self) -> str:
        return "%d.%d.%d" % (self[0], self[1], self[2])

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Version({self.major}, {self.minor}, {self.patch})"

    def at_least(self, other: Any) -> bool:
        """是否不低于 ``other``（``other`` 可为字符串/元组/Version）。"""
        return tuple(self) >= tuple(parse_version(other, product="generic"))


def _split(value: str) -> Tuple[int, int, int]:
    m = _VERSION_RE.match(value.strip())
    if not m:
        raise InvalidArgumentException(
            f"无法解析的版本号: {value!r}。示例: '5.7'、'5.7.44'、'8.0.36'、'9.1'")
    major = int(m.group(1))
    minor = int(m.group(2) or 0)
    patch = int(m.group(3) or 0)
    return major, minor, patch


def parse_version(value: Any, default: Optional[str] = None,
                  product: str = "mysql") -> Version:
    """把配置里的 ``version`` 归一为 :class:`Version`。

    ``product="generic"`` 时只解析不校验产品范围（供内部比较用）。
    ``default=None`` 时按产品取缺省版本（MySQL 5.7 / MongoDB 4.0）。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None:
            default = (DEFAULT_MONGO_VERSION if product == "mongodb"
                       else DEFAULT_MYSQL_VERSION)
        value = default

    if isinstance(value, Version):
        result = value
    elif isinstance(value, (tuple, list)):
        if len(value) not in (2, 3):
            raise InvalidArgumentException(
                f"版本元组必须是 (major, minor[, patch])，收到: {value!r}")
        try:
            nums = [int(v) for v in value]
        except (TypeError, ValueError):
            raise InvalidArgumentException(f"版本元组必须是数字: {value!r}")
        result = Version(*(nums + [0] * (3 - len(nums))))
    elif isinstance(value, bool):  # bool 是 int 子类，单独拦截
        raise InvalidArgumentException(f"非法版本号: {value!r}")
    elif isinstance(value, int):
        result = Version(value, 0, 0)
    elif isinstance(value, float):
        # 5.7 -> "5.7"（用 repr 避免 8.0 变 "8"）
        result = Version(*_split(repr(value)))
    elif isinstance(value, str):
        result = Version(*_split(value))
    else:
        raise InvalidArgumentException(
            f"版本号类型不支持: {type(value).__name__}，"
            f"请传 '8.0.36' / 8.0 / (8, 0, 36)")

    if product == "mysql":
        _validate_mysql(result)
    elif product == "mongodb":
        _validate_mongodb(result)
    return result


def _validate_mysql(version: Version) -> None:
    """MySQL 支持范围校验；越界立即报错，不做"就近猜测"。"""
    major, minor = version.major, version.minor
    if major == 5 and minor == 7:
        return
    if major == 8 and 0 <= minor <= 4:
        return
    if major == 9:
        return
    raise InvalidArgumentException(
        f"不支持的 MySQL 版本: {version}。支持范围: {MYSQL_SUPPORTED}；"
        f"未给 version 时默认按 {DEFAULT_MYSQL_VERSION}（5.7）处理")


def _validate_mongodb(version: Version) -> None:
    """MongoDB 支持范围校验；越界立即报错，不做"就近猜测"。

    支持线为官方长期支持版本：4.0 / 4.2 / 4.4 / 5.0 / 6.0 / 7.0 / 8.0。
    小版本号只允许偶数发布线（4.0/4.2/4.4），奇数小版本是"开发版"，
    不保证生产可用——与 MySQL 侧"5.7 / 8.0–8.4 / 9.x"的处理一致：
    放行真实发布线，拒绝不存在或未成型的版本。
    """
    major, minor = version.major, version.minor
    if major == 4 and minor in (0, 2, 4):
        return
    if major in (5, 6, 7, 8):
        return
    raise InvalidArgumentException(
        f"不支持的 MongoDB 版本: {version}。支持范围: {MONGO_SUPPORTED}；"
        f"未给 version 时默认按 {DEFAULT_MONGO_VERSION}（4.0）处理")
