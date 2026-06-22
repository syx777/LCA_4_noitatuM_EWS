# django__django-13807

## 问题背景

在 SQLite 后端的 `check_constraints` 方法中，当 `table_names` 参数包含 SQL 关键字（如 `order`）作为表名时，`loaddata` 命令会崩溃。报错信息：
```
sqlite3.OperationalError: near "order": syntax error
```
原因：`PRAGMA foreign_key_check(%s)` 等语句使用 `% table_name` 直接插入表名，未做任何引号处理。

**F2P 测试**：`test_check_constraints_sql_keywords` — 建立 `SQLKeywordsModel`（`db_table='order'`，PK 列 `db_column='select'`，FK 列 `db_column='where'`），注入无效 FK，调用 `check_constraints(table_names=['order'])` 期望抛出 `IntegrityError`。

**P2P 测试**：`test_check_constraints` — 使用普通命名的 `Article` 表（`backends_article`，普通列名），调用 `check_constraints()`（无 table_names 参数）期望抛出 `IntegrityError`。

## Golden Patch 语义分析

在 `check_constraints` 方法中，对 5 处 SQL 语句中的表名/列名插值添加 `self.ops.quote_name()` 引号处理：

```python
# 1. PRAGMA foreign_key_check
'PRAGMA foreign_key_check(%s)' % self.ops.quote_name(table_name)

# 2. PRAGMA foreign_key_list  
'PRAGMA foreign_key_list(%s)' % self.ops.quote_name(table_name)

# 3. SELECT query - 3 个插值点
'SELECT %s, %s FROM %s WHERE rowid = %%s' % (
    self.ops.quote_name(primary_key_column_name),
    self.ops.quote_name(column_name),
    self.ops.quote_name(table_name),
)
```

SQLite 的 `quote_name` 实现：`'"' + name + '"'`（标准 SQL 标识符引号）。

## 调用链分析

```
check_constraints(table_names=['order'])
  └─ PRAGMA foreign_key_check("order")         [Fix 1: 检查表是否有 FK 违规]
       └─ violations: [(table='order', rowid=X, ref='backends_reporter', fk_idx=0)]
  └─ for each violation:
       └─ PRAGMA foreign_key_list("order")      [Fix 2: 获取 FK 定义，找到列名]
            └─ foreign_key = list[0] -> ('where', 'id', ...) 
            └─ column_name='where', referenced_col='id'
       └─ get_primary_key_column -> 'select'     [introspection，无 SQL 注入风险]
       └─ SELECT "select", "where" FROM "order" WHERE rowid = X  [Fix 3: 获取违规行数据]
            └─ primary_key_value=1, bad_value=999
       └─ raise IntegrityError(...)              [预期结果]

check_constraints()  [P2P - 无 table_names]
  └─ PRAGMA foreign_key_check               [不传表名，不需要 quote]
       └─ violations: [(table='backends_article', ...)]
  └─ PRAGMA foreign_key_list(backends_article) [普通表名，无 keyword 问题]
  └─ SELECT id, reporter_id FROM backends_article WHERE rowid = X  [普通列名]
  └─ raise IntegrityError(...)
```

关键：P2P 测试中表名（`backends_article`）和列名（`id`、`reporter_id`）均不是 SQL 关键字，可以不加引号正常工作。F2P 测试中所有三个标识符（`order`、`select`、`where`）都是 SQL 关键字，不加引号会语法报错。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | 直接逆操作（去掉 fk_check 的 quote_name），A/B/C 三者完全相同 |
| B | 必须替换 | 替换 | 与 A 完全相同（重复 diff） |
| C | 必须替换 | 替换 | 与 A 完全相同（重复 diff） |
| D | 必须替换 | 替换 | 直接逆操作（去掉 fk_list 的 quote_name），也是简单还原 |
| E | — | 新增 | 无 E 组 |

全部 4 个已有 mutation 均需替换，新设计 5 个各覆盖不同的 quote_name 插值位置。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：去掉 `PRAGMA foreign_key_check` 的 `quote_name`（与 B、C 相同）。直接逆操作，质量极差。

**最终 mutation**（A1 — 去掉 PRAGMA foreign_key_check 的 quote_name）：
```diff
diff --git a/django/db/backends/sqlite3/base.py b/django/db/backends/sqlite3/base.py
index ab4ea70492..d1132733d5 100644
--- a/django/db/backends/sqlite3/base.py
+++ b/django/db/backends/sqlite3/base.py
@@ -329,7 +329,7 @@ class DatabaseWrapper(BaseDatabaseWrapper):
                     violations = chain.from_iterable(
                         cursor.execute(
                             'PRAGMA foreign_key_check(%s)'
-                            % self.ops.quote_name(table_name)
+                            % table_name
                         ).fetchall()
                         for table_name in table_names
```
**变异语义**：移除 `PRAGMA foreign_key_check` 中的表名引号处理。F2P FAIL：`PRAGMA foreign_key_check(order)` → SQLite OperationalError（`order` 是关键字，未引号包裹）。P2P PASS：`table_names=None` 时不执行此代码路径（直接用 `PRAGMA foreign_key_check` 无参数）。模拟开发者只修复了其中一处而遗漏了另外几处。

---

### Group B — 替换

**原 mutation**：去掉 `PRAGMA foreign_key_list` 的 `quote_name`（与原 D 相同）。质量差，直接逆操作。

**最终 mutation**（B1 — 去掉 PRAGMA foreign_key_list 的 quote_name）：
```diff
diff --git a/django/db/backends/sqlite3/base.py b/django/db/backends/sqlite3/base.py
index ab4ea70492..02a7aef0e7 100644
--- a/django/db/backends/sqlite3/base.py
+++ b/django/db/backends/sqlite3/base.py
@@ -336,7 +336,7 @@ class DatabaseWrapper(BaseDatabaseWrapper):
                 for table_name, rowid, referenced_table_name, foreign_key_index in violations:
                     foreign_key = cursor.execute(
-                        'PRAGMA foreign_key_list(%s)' % self.ops.quote_name(table_name)
+                        'PRAGMA foreign_key_list(%s)' % table_name
                     ).fetchall()[foreign_key_index]
```
**变异语义**：移除 `PRAGMA foreign_key_list` 中的引号处理。F2P FAIL：FK check 通过（`"order"` 正确引号）找到违规行，然后 `PRAGMA foreign_key_list(order)` → OperationalError。P2P PASS：`PRAGMA foreign_key_list(backends_article)` 无关键字问题，正常执行。模拟开发者只修复了 `foreign_key_check` 的引号问题，遗漏了 `foreign_key_list` 同样需要修复。

---

### Group C — 替换

**原 mutation**：同 A/B（重复 diff）。

**最终 mutation**（C1 — 去掉 SELECT 中 primary_key_column_name 的 quote_name）：
```diff
diff --git a/django/db/backends/sqlite3/base.py b/django/db/backends/sqlite3/base.py
index ab4ea70492..7b3cd4104a 100644
--- a/django/db/backends/sqlite3/base.py
+++ b/django/db/backends/sqlite3/base.py
@@ -342,7 +342,7 @@ class DatabaseWrapper(BaseDatabaseWrapper):
                     primary_key_value, bad_value = cursor.execute(
                         'SELECT %s, %s FROM %s WHERE rowid = %%s' % (
-                            self.ops.quote_name(primary_key_column_name),
+                            primary_key_column_name,
                             self.ops.quote_name(column_name),
                             self.ops.quote_name(table_name),
```
**变异语义**：PRAGMA 引号正确，但 SELECT 语句中 PK 列名未引号。F2P FAIL：`pk_col = 'select'` → `SELECT select, "where" FROM "order" WHERE rowid = ?` → `near "select": syntax error`。P2P PASS：`pk_col = 'id'` → `SELECT id, ...` 正常（id 不是关键字）。模拟开发者修复了 PRAGMA 和表名，但遗漏了 SELECT 中的 PK 列名。

---

### Group D — 替换

**原 mutation**：去掉 `PRAGMA foreign_key_list` 的 `quote_name`（直接逆操作）。

**最终 mutation**（D1 — 去掉 SELECT 中 column_name 的 quote_name）：
```diff
diff --git a/django/db/backends/sqlite3/base.py b/django/db/backends/sqlite3/base.py
index ab4ea70492..cbf27adcb0 100644
--- a/django/db/backends/sqlite3/base.py
+++ b/django/db/backends/sqlite3/base.py
@@ -343,7 +343,7 @@ class DatabaseWrapper(BaseDatabaseWrapper):
                         'SELECT %s, %s FROM %s WHERE rowid = %%s' % (
                             self.ops.quote_name(primary_key_column_name),
-                            self.ops.quote_name(column_name),
+                            column_name,
                             self.ops.quote_name(table_name),
```
**变异语义**：PRAGMA 和 PK 列引号正确，但 SELECT 中 FK 列名未引号。F2P FAIL：`col = 'where'` → `SELECT "select", where FROM "order" WHERE rowid = ?` → `near "where": syntax error`。P2P PASS：`col = 'reporter_id'` 正常（不是关键字）。模拟开发者修复了除 FK 列名外的所有地方。

---

### Group E — 新增

**最终 mutation**（E2 — 去掉 SELECT 中 table_name 的 quote_name）：
```diff
diff --git a/django/db/backends/sqlite3/base.py b/django/db/backends/sqlite3/base.py
index ab4ea70492..33d40ef9a4 100644
--- a/django/db/backends/sqlite3/base.py
+++ b/django/db/backends/sqlite3/base.py
@@ -344,7 +344,7 @@ class DatabaseWrapper(BaseDatabaseWrapper):
                         'SELECT %s, %s FROM %s WHERE rowid = %%s' % (
                             self.ops.quote_name(primary_key_column_name),
                             self.ops.quote_name(column_name),
-                            self.ops.quote_name(table_name),
+                            table_name,
```
**变异语义**：PRAGMA 和列名引号正确，但 SELECT 的 FROM 子句中表名未引号。F2P FAIL：`table = 'order'` → `SELECT "select", "where" FROM order WHERE rowid = ?` → `near "order": syntax error`。P2P PASS：`table = 'backends_article'` 正常（不是关键字）。最难发现的 mutation：所有 PRAGMA 和列名都正确引号，只有 SELECT 的 FROM 子句遗漏，需要专门针对关键字表名的 SELECT 测试才能检测到。

---

## 新设计 Mutation 说明

### 设计原则
Golden patch 一共在 5 处添加了 `quote_name`，分别对应：
1. `PRAGMA foreign_key_check` 的表名（A）
2. `PRAGMA foreign_key_list` 的表名（B）
3. SELECT 语句的 PK 列名（C）
4. SELECT 语句的 FK 列名（D）
5. SELECT 语句的表名（E）

每个 mutation 只移除其中一处，其余 4 处保持正确。P2P 测试使用普通 Django 应用程序表名（`backends_article`）和列名（`id`, `reporter_id`），这些均不是 SQL 关键字，不加引号也可以正常工作，因此 P2P 通过。F2P 测试使用 SQL 关键字（`order`, `select`, `where`），任何一处未加引号都会触发 OperationalError，不是预期的 IntegrityError，因此 F2P 失败。

这 5 个 mutation 直接映射了代码审查中最常见的漏洞：开发者在修复 SQL 注入/关键字冲突时漏掉了某一个插值点。
