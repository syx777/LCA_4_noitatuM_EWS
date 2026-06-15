# django__django-12741

## 问题背景

`execute_sql_flush` 方法接受一个显式的 `using` 参数（数据库别名），但调用方 `flush.py` 的 `handle()` 传入的是 `database` 变量（来自命令行参数）。这造成了耦合：方法本身已经通过 `self.connection` 绑定到特定连接，再接受外部传入的 `using` 参数是多余的，而且如果调用方传错别名，会在错误的连接上执行事务。

Golden patch 的修复：
1. 移除 `execute_sql_flush` 的 `using` 参数，改为使用 `self.connection.alias` 获取正确的别名。
2. 将 `flush.py` 的调用从 `connection.ops.execute_sql_flush(database, sql_list)` 改为 `connection.ops.execute_sql_flush(sql_list)`。

## Golden Patch 语义分析

```python
# 修复前
def execute_sql_flush(self, using, sql_list):
    with transaction.atomic(using=using, ...):  # 使用外部传入的 using

# 修复后
def execute_sql_flush(self, sql_list):
    with transaction.atomic(
        using=self.connection.alias,  # 使用 self.connection 的别名，保证一致性
        savepoint=self.connection.features.can_rollback_ddl,
    ):
        with self.connection.cursor() as cursor:
            for sql in sql_list:
                cursor.execute(sql)
```

核心语义：`execute_sql_flush` 通过 `self.connection` 执行游标操作，`transaction.atomic` 的 `using` 参数必须与 `self.connection` 指向同一数据库。原代码将这两者解耦（外部传入 using），有可能造成不匹配。修复后两者通过 `self.connection.alias` 保证一致。

`savepoint=self.connection.features.can_rollback_ddl` 控制是否在事务内使用 SAVEPOINT。对于不支持 DDL 回滚的数据库（如 MySQL），`can_rollback_ddl=False`，禁止 SAVEPOINT 避免错误；对于支持的数据库（PostgreSQL），使用 SAVEPOINT 以支持嵌套事务。

## 调用链分析

```
flush.py: Command.handle()
  ├── sql_list = sql_flush(style, connection, ...)     # 生成 TRUNCATE/DELETE SQL
  └── connection.ops.execute_sql_flush(sql_list)       # 执行 flush SQL（修复后）
        └── transaction.atomic(using=self.connection.alias, savepoint=...)
              └── self.connection.cursor() as cursor
                    └── cursor.execute(sql)  for each sql in sql_list

tests/backends/base/test_operations.py:
  └── connection.ops.execute_sql_flush(sql_list)       # F2P 测试直接调用
        └── [same as above]
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | 直接还原旧签名 `execute_sql_flush(self, using, sql_list)`，逆操作 golden patch |
| B | 必须替换 | 替换 | `sql_list[1:]  # Bug: skip first statement`，注释中写了 "Bug"，极不自然 |
| C | 必须替换 | 替换（缺失） | C 组不存在于 mutations.jsonl，需新设计 |
| D | 必须替换 | 替换 | 添加 `if not hasattr(self, "_flush_initialized"): raise RuntimeError(...)` + 移除 `using`，两处不相关改动，人工痕迹明显 |
| E | 必须替换 | 替换 | 添加无意义的 `execute=False` 参数并用 `if execute:` 包裹执行，等同于让方法默认不做任何事 |

语义浅层共 0 个。全部为必须替换。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：还原 `execute_sql_flush(self, using, sql_list)` 签名，等效于逆操作 golden patch。

**分类**：🔴 必须替换

**最终 mutation**：
```diff
diff --git a/django/db/backends/base/operations.py b/django/db/backends/base/operations.py
index 305306e8ce..2e8327db33 100644
--- a/django/db/backends/base/operations.py
+++ b/django/db/backends/base/operations.py
@@ -403,7 +403,7 @@ class BaseDatabaseOperations:
     def execute_sql_flush(self, sql_list):
         """Execute a list of SQL statements to flush the database."""
         with transaction.atomic(
-            using=self.connection.alias,
+            using=self.connection.vendor,
             savepoint=self.connection.features.can_rollback_ddl,
         ):
             with self.connection.cursor() as cursor:
```

**变异语义**：将 `transaction.atomic` 的 `using` 参数从 `self.connection.alias`（如 `'default'`）改为 `self.connection.vendor`（如 `'sqlite'`、`'postgresql'`）。`transaction.atomic(using=X)` 会在 `settings.DATABASES` 中查找别名 `X`；`vendor` 是数据库引擎类型而非别名，不在 DATABASES 中，导致 `ConnectionDoesNotExist` 异常。对于了解 Django 的开发者，`alias` 和 `vendor` 都是 connection 的字符串属性，很容易混淆。

---

### Group B — 替换

**原 mutation**：`for sql in sql_list[1:]:  # Bug: skip first statement`，注释中显式写了 "Bug"。

**分类**：🔴 必须替换

**最终 mutation**：
```diff
diff --git a/django/db/backends/base/operations.py b/django/db/backends/base/operations.py
index 305306e8ce..79be23417a 100644
--- a/django/db/backends/base/operations.py
+++ b/django/db/backends/base/operations.py
@@ -404,7 +404,7 @@ class BaseDatabaseOperations:
         """Execute a list of SQL statements to flush the database."""
         with transaction.atomic(
             using=self.connection.alias,
-            savepoint=self.connection.features.can_rollback_ddl,
+            savepoint=not self.connection.features.can_rollback_ddl,
         ):
             with self.connection.cursor() as cursor:
                 for sql in sql_list:
```

**变异语义**：将 savepoint 参数从 `can_rollback_ddl` 取反。对于 SQLite（`can_rollback_ddl=False`），变为 `savepoint=True`，强制在 DDL 操作上使用 SAVEPOINT。SQLite 不支持 DDL 回滚（`TRUNCATE` 等操作在 SQLite 中是通过 `DELETE` 实现的），强制使用 SAVEPOINT 会改变事务嵌套行为，在某些情况下导致操作失败或数据未被清除。对于 PostgreSQL（`can_rollback_ddl=True`），变为 `savepoint=False`，禁止原本应有的 SAVEPOINT，破坏嵌套事务安全性。

---

### Group C — 替换（新设计）

**原 mutation**：（不存在，需新设计）

**分类**：🔴 必须替换（缺失）

**最终 mutation**：
```diff
diff --git a/django/db/backends/base/operations.py b/django/db/backends/base/operations.py
index 305306e8ce..30b12b17bc 100644
--- a/django/db/backends/base/operations.py
+++ b/django/db/backends/base/operations.py
@@ -402,11 +402,11 @@ class BaseDatabaseOperations:
 
     def execute_sql_flush(self, sql_list):
         """Execute a list of SQL statements to flush the database."""
-        with transaction.atomic(
-            using=self.connection.alias,
-            savepoint=self.connection.features.can_rollback_ddl,
-        ):
-            with self.connection.cursor() as cursor:
+        with self.connection.cursor() as cursor:
+            with transaction.atomic(
+                using=self.connection.alias,
+                savepoint=self.connection.features.can_rollback_ddl,
+            ):
                 for sql in sql_list:
                     cursor.execute(sql)
```

**变异语义**：交换 cursor 和 transaction.atomic 的嵌套顺序：原本 cursor 在 atomic 内部（cursor 生命周期受事务管理），变为 cursor 在 atomic 外部（cursor 在事务开始前创建）。这改变了资源获取顺序的语义契约：事务应当"包含"cursor 的使用，反转后 cursor 在事务作用域之外打开，可能导致：(1) 在某些数据库后端，cursor 在事务开始前就与连接状态绑定，后续事务操作无效；(2) 事务回滚时，已通过该 cursor 执行的操作可能不被回滚。这模拟了开发者认为"游标复用可以提升性能"而将资源获取提到事务外的真实错误。

---

### Group D — 替换

**原 mutation**：添加假初始化检查 `if not hasattr(self, "_flush_initialized"): raise RuntimeError(...)` 且移除 `using=self.connection.alias`，两处改动都不自然。

**分类**：🔴 必须替换

**最终 mutation**：
```diff
diff --git a/django/db/backends/base/operations.py b/django/db/backends/base/operations.py
index 305306e8ce..8287835052 100644
--- a/django/db/backends/base/operations.py
+++ b/django/db/backends/base/operations.py
@@ -403,7 +403,7 @@ class BaseDatabaseOperations:
     def execute_sql_flush(self, sql_list):
         """Execute a list of SQL statements to flush the database."""
         with transaction.atomic(
-            using=self.connection.alias,
+            using=self.connection.settings_dict['NAME'],
             savepoint=self.connection.features.can_rollback_ddl,
         ):
             with self.connection.cursor() as cursor:
```

**变异语义**：将 `using` 参数从 `self.connection.alias`（数据库别名，如 `'default'`）改为 `self.connection.settings_dict['NAME']`（数据库文件名或主机名，如 `':memory:'`、`'/path/to/db.sqlite3'` 或 `'mydb'`）。`settings_dict['NAME']` 是 Django 连接配置中的数据库名称，不是别名，`transaction.atomic(using=':memory:')` 会因找不到该别名而抛出 `ConnectionDoesNotExist`。这模拟了开发者混淆"数据库名称"和"Django 数据库别名"的常见误解——两者都是字符串，语义上相关但功能不同。

---

### Group E — 替换

**原 mutation**：添加 `execute=False` 默认参数并用 `if execute:` 包裹执行逻辑，等同于让方法默认不执行任何 SQL，并在 flush.py 里传 `execute=True`。

**分类**：🔴 必须替换

**最终 mutation**：
```diff
diff --git a/django/db/backends/base/operations.py b/django/db/backends/base/operations.py
index 305306e8ce..2320295d99 100644
--- a/django/db/backends/base/operations.py
+++ b/django/db/backends/base/operations.py
@@ -404,7 +404,7 @@ class BaseDatabaseOperations:
         """Execute a list of SQL statements to flush the database."""
         with transaction.atomic(
             using=self.connection.alias,
-            savepoint=self.connection.features.can_rollback_ddl,
+            savepoint=True,
         ):
             with self.connection.cursor() as cursor:
                 for sql in sql_list:
```

**变异语义**：将 `savepoint` 参数硬编码为 `True`，始终强制使用 SAVEPOINT，忽略数据库的 `can_rollback_ddl` 特性。对于不支持 DDL 回滚的数据库（MySQL 的 `can_rollback_ddl=False`），强制 SAVEPOINT 会导致 DDL 语句（如 TRUNCATE）自动提交，绕过事务包装，使得后续的回滚变得不可预测。对于 PostgreSQL 等支持 DDL 回滚的数据库（`can_rollback_ddl=True`），行为不变（原本也是 `True`）。这是数据库特性检测的语义变异：看似无害的 `True` 替换实际上忽略了关键的数据库能力判断。

## 新设计 Mutation 说明

### Group C（新设计）
基于对 `execute_sql_flush` 的资源管理模式分析：该方法的正确结构是 `atomic → cursor → execute`，体现了"事务包含游标使用"的语义契约。交换嵌套顺序虽然代码看起来仍然合法，但改变了事务作用域与游标生命周期的关系。这模拟了开发者为"优化游标复用"而调整代码结构的真实错误——一个看似合理的重构操作，实际上打破了资源获取顺序的语义约定。
