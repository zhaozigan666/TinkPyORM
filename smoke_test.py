# -*- coding: utf-8 -*-
"""冒烟测试：验证核心链路。"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from thinkorm import Db, Model, raw

Db.set_config({':memory:': ':memory:'} if False else '')
# 使用独立连接
from thinkorm.connection import Connection
Db.set_config(Connection(':memory:'))

# 建表
Db.execute('''
CREATE TABLE user (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    email TEXT,
    age INTEGER DEFAULT 0,
    status INTEGER DEFAULT 1,
    tags TEXT,
    create_time TEXT,
    update_time TEXT
)
''')
Db.execute('''
CREATE TABLE article (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    title TEXT,
    content TEXT
)
''')

# ============ Db + 查询构造器 ============
Db.table('user').insert({'name': 'thinkphp', 'email': 'thinkphp@qq.com', 'age': 30})
Db.table('user').insert_all([
    {'name': 'topthink', 'email': 'topthink@qq.com', 'age': 25},
    {'name': 'alice', 'email': 'alice@qq.com', 'age': 20},
    {'name': 'bob', 'email': 'bob@qq.com', 'age': 35},
])

# where 各种形式
assert Db.table('user').where('id', 1).value('name') == 'thinkphp'
assert Db.table('user').where('id', '>', 1).count() == 3
assert Db.table('user').where({'age': 20}).value('name') == 'alice'
assert Db.table('user').where_in('id', [1, 2]).count() == 2
assert Db.table('user').where_like('name', 'top%').value('name') == 'topthink'
assert Db.table('user').where_between('age', [20, 30]).count() == 3
assert Db.table('user').where_null('email').count() == 0

# 链式
rows = Db.table('user').field('id,name').where('age', '>', 20).order('age', 'desc').limit(2).select()
assert len(rows) == 2
assert rows[0]['name'] == 'bob'
assert 'email' not in rows[0]

# 聚合
assert Db.table('user').sum('age') == 110
assert Db.table('user').avg('age') == 27.5
assert Db.table('user').max('age') == 35
assert Db.table('user').min('age') == 20

# 原生表达式 + update + inc
Db.table('user').where('id', 1).inc('age', 5)
assert Db.table('user').where('id', 1).value('age') == 35

# join
Db.table('article').insert({'user_id': 1, 'title': 'hello', 'content': 'world'})
joined = Db.table('user u').join('article a', lambda q: q.where_column('a.user_id', '=', 'u.id')) \
    .field('u.name', 'a.title').select()
assert len(joined) == 1 and joined[0]['title'] == 'hello'

# group + having
stats = Db.table('user').field('status').group('status').having('COUNT(*)', '>', 0).select()
assert len(stats) == 1

# fetch_sql
sql = Db.table('user').where('id', 1).fetch_sql().select()
assert 'SELECT' in sql and 'WHERE' in sql and '1' in sql

# 事务
Db.transaction(lambda: (Db.table('user').insert({'name': 'tx1'}),
                        Db.table('user').insert({'name': 'tx2'})))
assert Db.table('user').where_like('name', 'tx%').count() == 2

# 事务回滚
try:
    with Db.transaction():
        Db.table('user').insert({'name': 'tx3'})
        raise RuntimeError('rollback')
except RuntimeError:
    pass
assert Db.table('user').where('name', 'tx3').count() == 0

# ============ 模型 ============
class User(Model):
    __table__ = 'user'
    __timestamps__ = True
    __type__ = {'tags': 'json'}
    __json__ = ['tags']

    def articles(self):
        return self.has_many('Article', 'user_id')

    def get_name_attr(self, value):
        return value.upper() if value else value

    def scope_active(self, query):
        query.where('status', 1)

class Article(Model):
    __table__ = 'article'

    def user(self):
        return self.belongs_to('User', 'user_id')

# create / find / save
u = User.create({'name': 'model_user', 'age': 40, 'tags': ['a', 'b']})
assert u.id is not None
assert u.create_time is not None and u.update_time is not None
assert User.find(u.id).tags == ['a', 'b']
assert User.get(u.id).name == 'MODEL_USER'  # 获取器

# update via instance
u = User.find(u.id)
u.name = 'model_renamed'
u.save()
assert User.find(u.id).name == 'MODEL_RENAMED'

# 静态 update / destroy
User.update({'age': 99}, {'id': u.id})
assert User.find(u.id).age == 99
User.destroy(u.id)
assert User.find(u.id) is None

# destroy 批量
User.create({'name': 'd1'})
User.create({'name': 'd2'})
User.destroy([x.id for x in User.where_like('name', 'd%').select()])
assert User.where_like('name', 'd%').count() == 0

# 查询范围 + 链式
User.create({'name': 'scope1', 'status': 1})
User.create({'name': 'scope2', 'status': 0})
assert User.active().where_in('name', ['scope1', 'scope2']).count() == 1
assert User.active().where('name', 'scope1').count() == 1
assert User.active().where('name', 'scope2').count() == 0

# camelCase 别名
assert User.whereIn('id', [x.id for x in User.select()]).count() == User.count()
assert User.findOrEmpty(999999) is not None

# ============ 关联 ============
u = User.create({'name': 'rel_user'})
a1 = Article.create({'user_id': u.id, 'title': 't1'})
a2 = Article.create({'user_id': u.id, 'title': 't2'})
assert len(u.articles) == 2          # 懒加载属性访问
assert u.articles[0].title in ('t1', 't2')
assert Article.find(a1.id).user.name == 'REL_USER'  # belongs_to

# 预载入 with
users = User.with_('articles').where('id', u.id).select()
assert len(users[0].articles) == 2

# together 级联保存
new_user = User()
new_user.name = 'together_user'
new_user.together(['articles'])
new_user.articles = [Article(title='c1'), Article(title='c2')]
new_user.save()
assert Article.where('user_id', new_user.id).count() == 2

# 分页
page = User.paginate(2, 1)
assert page.total == User.count()
assert len(page.items) == 2

# SQL 日志
assert len(Db.get_sql_log()) > 0

print("冒烟测试全部通过 ✔")
