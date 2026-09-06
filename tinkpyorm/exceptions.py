"""tinkpyorm 异常体系（参考 think-orm v4.0 的异常分层）。"""


class OrmError(Exception):
    """ORM 基础异常，所有其他异常的父类。"""


class QueryError(OrmError):
    """查询构造/执行错误。"""


class DataNotFound(OrmError):
    """数据未找到（Db 查询层，find/select 无结果时）。"""


class ModelNotFound(DataNotFound):
    """模型数据未找到（Model 层 find 无结果时）。"""


class RelationNotFound(OrmError):
    """关联未定义错误。"""


class TransactionError(OrmError):
    """事务操作错误。"""


class InvalidArgumentException(OrmError):
    """参数错误。"""


class ConfigError(OrmError):
    """配置错误：连接未注册、配置项非法、驱动不可用等。"""


class DriverNotAvailable(ConfigError):
    """驱动未安装或未提供实现（如未安装 pymysql 时使用 mysql 驱动）。"""


class ConnectionNotFound(ConfigError, KeyError):
    """具名连接未注册。

    同时继承 ``KeyError`` 以兼容旧版本 ``Db.get_connection()`` 抛出
    ``KeyError`` 的行为，旧调用方的 ``except KeyError`` 仍然成立。
    """

    def __str__(self) -> str:  # KeyError.__str__ 会给消息加引号，此处还原
        return self.args[0] if self.args else ""
