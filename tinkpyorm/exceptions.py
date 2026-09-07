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


class DriverNotAvailable(OrmError):
    """驱动未安装或未提供实现（如未安装 pymysql 时使用 mysql 驱动）。"""
