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


class DuplicateKeyError(OrmError):
    """唯一键 / 唯一索引冲突（如 MongoDB 的重复 _id）。"""


class ConnectionFailedError(OrmError):
    """数据库连接失败、超时或服务端不可用。

    v0.12.0 引入：MongoDB 驱动把 ``pymongo`` 的连接类异常
    （连接失败 / 服务选择超时 / 网络超时）统一转换为本异常，
    使调用方无需 import 第三方库即可捕获。
    """
