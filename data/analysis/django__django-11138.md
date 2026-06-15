# django__django-11138

## 问题背景

DATABASES 配置中的 `TIME_ZONE` 值在 MySQL、SQLite 和 Oracle 后端进行日期时区转换时未被正确使用。

当用户在 `DATABASES` 中为某个数据库连接单独设置 `TIME_ZONE`（例如指向一个存储非 UTC 时间的遗留数据库），`datetime__date` 等查询过滤时，Django 仍然硬编码从 `'UTC'` 转换到目标时区，而非从连接时区（`connection.timezone_name`）转换，导致日期过滤结果错误。

golden patch 的修复：
- **MySQL**：`_convert_field_to_tz` 从 `CONVERT_TZ(field, 'UTC', tzname)` 改为 `CONVERT_TZ(field, connection.timezone_name, tzname)`，并增加 `connection.timezone_name != tzname` 短路条件（相同则跳过转换）。
- **Oracle**：`_convert_field_to_tz` 从 `FROM_TZ(field, '0:00')` 改为 `FROM_TZ(field, connection.timezone_name)`，并增加相同时区短路逻辑。
- **SQLite**：`_sqlite_datetime_parse` 增加 `conn_tzname` 参数，先用 `dt.replace(tzinfo=...)` 设置连接时区，再用 `timezone.localtime` 转换到目标时区（且仅在两者不同时才转换）。`_convert_tznames_to_sql` 重命名并同时返回目标时区和连接时区两个参数，供 SQLite 自定义函数使用。

## Golden Patch 语义分析

核心修复逻辑：**"从连接时区转换到查询目标时区，而不是从硬编码的 UTC 转换"**。

同时引入了一个重要优化：当连接时区与目标时区相同时，跳过转换，避免不必要的 SQL 函数调用（对 MySQL 而言 `CONVERT_TZ` 在时区相同时也可能改变行为）。

SQLite 的修复比其他后端更深层：不仅修改了 SQL 生成层（`_convert_tznames_to_sql`），还修改了 Python 端的自定义函数实现（`_sqlite_datetime_parse`），因为 SQLite 的时区处理完全由 Python 自定义函数完成。

## 调用链分析

```
datetime__date 查询 (ORM lookup)
  → DatabaseOperations.datetime_cast_date_sql(field_name, tzname)
      [MySQL]  → _convert_field_to_tz(field_name, tzname)
                   → 生成 SQL: CONVERT_TZ(field, conn_tz, tzname) 或直接 field
      [Oracle] → _convert_field_to_tz(field_name, tzname)
                   → 生成 SQL: CAST((FROM_TZ(field, conn_tz) AT TIME ZONE tzname) AS TIMESTAMP)
      [SQLite] → _convert_tznames_to_sql(tzname) → (tzname_sql, conn_tzname_sql)
                   → 生成 SQL: django_datetime_cast_date(field, tzname, conn_tzname)
                       → [Python 自定义函数] _sqlite_datetime_cast_date(dt, tzname, conn_tzname)
                           → _sqlite_datetime_parse(dt, tzname, conn_tzname)
                               1. typecast_timestamp(dt)  # 解析原始时间串
                               2. dt.replace(tzinfo=pytz.timezone(conn_tzname))  # 设置连接时区
                               3. timezone.localtime(dt, pytz.timezone(tzname))  # 转换到目标时区
```

关键数据流：`tzname`（Django 当前时区）和 `conn_tzname`（数据库连接时区）分别由两个不同的配置来源获得，golden patch 确保两者都被正确传入转换函数。

## 替换决策总览

数据来源说明：mutations.jsonl 中仅有1条 `instance_id=django__django-11138` 的记录（Group B），属于部分数据。本次为所有5个策略组各设计一个高质量 mutation。

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 新设计 | 新增 | MySQL 转换方向反转：from/to 时区参数互换 |
| B | 必须替换 | 替换 | 原 mutation 为两个独立的逻辑取反，明显人工痕迹且语义浅层 |
| C | 新设计 | 新增 | SQLite base.py 中操作顺序颠倒，导致 conn_tzname 覆盖已转换的 dt |
| D | 新设计 | 新增 | Oracle 短路条件取反，跳过必要转换而执行不必要的转换 |
| E | 新设计 | 新增 | SQLite base.py 中 localtime 调用条件取反，仅在相同时区时才转换 |

**原 Group B mutation 评估**：
- 在 MySQL 中将 `and` 改为 `or`（使得无论 timezone_name 是否等于 tzname 都会执行转换）
- 在 SQLite 中将 `if settings.USE_TZ:` 改为 `if not settings.USE_TZ:`（直接逻辑取反）
- 分类：🔴 **必须替换** — 两处修改都属于对 golden patch 核心逻辑的直接取反，且 SQLite 部分会将 `_convert_tznames_to_sql` 行为完全反转（USE_TZ=True 时返回 NULL），属于"功能等价冗余"和"不自然"的组合。

## 各组 Mutation 分析

### Group A — 替换（新设计）
**原 mutation**：无（新设计）
**分类**：新设计 - 跨函数变异（MySQL 转换方向语义错误）
**理由**：MySQL 的 `CONVERT_TZ(field, from_tz, to_tz)` 语义是从 from_tz 转到 to_tz。Golden patch 将参数顺序从 `(field, conn_tz, tzname)` 修正为正确顺序，mutation 将其改为 `(field, tzname, conn_tz)`，即从目标时区反向转换到连接时区，方向完全相反。

**最终 mutation**：
```diff
diff --git a/django/db/backends/mysql/operations.py b/django/db/backends/mysql/operations.py
index da15e79ec2..d4055c568a 100644
--- a/django/db/backends/mysql/operations.py
+++ b/django/db/backends/mysql/operations.py
@@ -70,7 +70,7 @@ class DatabaseOperations(BaseDatabaseOperations):
 
     def _convert_field_to_tz(self, field_name, tzname):
         if settings.USE_TZ and self.connection.timezone_name != tzname:
-            field_name = "CONVERT_TZ(%s, '%s', '%s')" % (field_name, self.connection.timezone_name, tzname)
+            field_name = "CONVERT_TZ(%s, '%s', '%s')" % (field_name, tzname, self.connection.timezone_name)
         return field_name
 
     def datetime_cast_date_sql(self, field_name, tzname):
```
**变异语义**：在连接时区与查询时区不同时，MySQL 将执行反向转换（从 tzname 转到 conn_tz），而非正向（从 conn_tz 转到 tzname）。当两者相同时行为正常（短路条件跳过）。代码看起来只是两个字符串变量顺序互换，非常难以在 code review 中发现。`test_query_convert_timezones` 中时区不同时的测试用例（Asia/Bangkok vs Africa/Nairobi）会失败，时区相同的场景（默认情况）通过。

---

### Group B — 替换（新设计）
**原 mutation**：
```diff
diff --git a/django/db/backends/mysql/operations.py b/django/db/backends/mysql/operations.py
index da15e79ec2..275fd7fbcd 100644
--- a/django/db/backends/mysql/operations.py
+++ b/django/db/backends/mysql/operations.py
@@ -69,7 +69,7 @@ class DatabaseOperations(BaseDatabaseOperations):
             return "DATE(%s)" % (field_name)
 
     def _convert_field_to_tz(self, field_name, tzname):
-        if settings.USE_TZ and self.connection.timezone_name != tzname:
+        if settings.USE_TZ or self.connection.timezone_name != tzname:
             field_name = "CONVERT_TZ(%s, '%s', '%s')" % (field_name, self.connection.timezone_name, tzname)
         return field_name
...
+        if not settings.USE_TZ:
             return "'%s'" % tzname, "'%s'" % self.connection.timezone_name
```
**分类**：🔴 必须替换 — 两处修改均为直接逻辑取反，且 SQLite 部分在 USE_TZ=True（标准配置）时返回 NULL 会造成 SQLite 函数参数不足而崩溃，属于"不自然"的 mutation（SQLite 后端的格式字符串接收3个参数但 NULL 只用于 USE_TZ=False）。
**理由**：需要设计更隐蔽、单一的 mutation。

**最终 mutation**（新设计：SQLite `_convert_tznames_to_sql` 中 tzname 和 conn_tzname 返回顺序互换）：
```diff
diff --git a/django/db/backends/sqlite3/operations.py b/django/db/backends/sqlite3/operations.py
index 364b3eba05..d7a2778748 100644
--- a/django/db/backends/sqlite3/operations.py
+++ b/django/db/backends/sqlite3/operations.py
@@ -86,7 +86,7 @@ class DatabaseOperations(BaseDatabaseOperations):
 
     def _convert_tznames_to_sql(self, tzname):
         if settings.USE_TZ:
-            return "'%s'" % tzname, "'%s'" % self.connection.timezone_name
+            return "'%s'" % self.connection.timezone_name, "'%s'" % tzname
         return 'NULL', 'NULL'
 
     def datetime_cast_date_sql(self, field_name, tzname):
```
**变异语义**：`_convert_tznames_to_sql` 返回的两个时区参数位置互换。SQLite 自定义函数接收 `(dt, tzname, conn_tzname)` 顺序，此 mutation 导致 `tzname` 和 `conn_tzname` 实参被互换传入，`_sqlite_datetime_parse` 将把数据库连接时区当作目标时区来转换，把查询时区当作连接时区来设置 tzinfo。当两者相同时无影响，当不同时转换方向和基准都会错误。

---

### Group C — 替换（新设计）
**原 mutation**：无（新设计）
**分类**：新设计 - 初始化/状态顺序变异
**理由**：SQLite `_sqlite_datetime_parse` 的核心逻辑依赖严格的操作顺序：先设置时区信息（`replace`），再做时区本地化（`localtime`）。交换顺序会导致：先对一个可能无时区信息的 datetime 执行 `localtime`（可能失败或使用错误 UTC 假设），再用 `replace` 覆盖已正确转换的 tzinfo。

**最终 mutation**：
```diff
diff --git a/django/db/backends/sqlite3/base.py b/django/db/backends/sqlite3/base.py
index 24d07cc11a..5caffc4a1f 100644
--- a/django/db/backends/sqlite3/base.py
+++ b/django/db/backends/sqlite3/base.py
@@ -405,10 +405,10 @@ def _sqlite_datetime_parse(dt, tzname=None, conn_tzname=None):
         dt = backend_utils.typecast_timestamp(dt)
     except (TypeError, ValueError):
         return None
-    if conn_tzname:
-        dt = dt.replace(tzinfo=pytz.timezone(conn_tzname))
     if tzname is not None and tzname != conn_tzname:
         dt = timezone.localtime(dt, pytz.timezone(tzname))
+    if conn_tzname:
+        dt = dt.replace(tzinfo=pytz.timezone(conn_tzname))
     return dt
```
**变异语义**：操作顺序颠倒。原逻辑：parse → 设conn_tz → 转到目标tz。变异后：parse → 转到目标tz（此时 dt 为 naive，`timezone.localtime` 将对其假设为 UTC）→ 用 conn_tzname 的 replace 覆盖 tzinfo（破坏已转换的结果）。对于有连接时区配置的场景（`test_query_convert_timezones` 的两个分支），结果都会错误，但普通的 USE_TZ=True + UTC 连接的简单测试可能通过。

---

### Group D — 替换（新设计）
**原 mutation**：无（新设计）
**分类**：新设计 - 接口契约变异（Oracle 短路条件取反）
**理由**：Oracle `_convert_field_to_tz` 的短路条件 `if self.connection.timezone_name != tzname` 意味着"只有在需要转换时才生成 CAST...FROM_TZ 表达式"。取反为 `==` 后，语义变为：相同时区时做一次无意义的自转换，不同时区时直接返回原字段（完全不转换）。

**最终 mutation**：
```diff
diff --git a/django/db/backends/oracle/operations.py b/django/db/backends/oracle/operations.py
index 77d330c411..4f9f13ff98 100644
--- a/django/db/backends/oracle/operations.py
+++ b/django/db/backends/oracle/operations.py
@@ -102,7 +102,7 @@ END;
         # Convert from connection timezone to the local time, returning
         # TIMESTAMP WITH TIME ZONE and cast it back to TIMESTAMP to strip the
         # TIME ZONE details.
-        if self.connection.timezone_name != tzname:
+        if self.connection.timezone_name == tzname:
             return "CAST((FROM_TZ(%s, '%s') AT TIME ZONE '%s') AS TIMESTAMP)" % (
                 field_name,
                 self.connection.timezone_name,
```
**变异语义**：当连接时区与查询时区相同时（正常情况），会执行一次从 conn_tz 到 conn_tz 的自转换（语义上无害但多余）；当两者不同时（需要转换的情况），直接返回原字段不做转换，导致时区错误。代码看起来是一个常见的 `!=` vs `==` 的边界判断，审查时很容易忽略。`test_query_convert_timezones` 中连接时区与查询时区不同的场景会失败。

---

### Group E — 替换（新设计）
**原 mutation**：无（新设计）
**分类**：新设计 - 条件组合变异（SQLite localtime 调用条件取反）
**理由**：`_sqlite_datetime_parse` 中 `if tzname is not None and tzname != conn_tzname` 这个条件确保"仅在目标时区与连接时区不同时才做 localtime 转换"（相同则无需转换）。将 `!=` 改为 `==`，含义变为"仅在相同时才转换"，与直觉完全相反——实际上在需要转换的场景（时区不同）跳过了转换，在不需要转换的场景（时区相同）执行了多余的转换。

**最终 mutation**：
```diff
diff --git a/django/db/backends/sqlite3/base.py b/django/db/backends/sqlite3/base.py
index 24d07cc11a..3897ae780c 100644
--- a/django/db/backends/sqlite3/base.py
+++ b/django/db/backends/sqlite3/base.py
@@ -407,7 +407,7 @@ def _sqlite_datetime_parse(dt, tzname=None, conn_tzname=None):
         return None
     if conn_tzname:
         dt = dt.replace(tzinfo=pytz.timezone(conn_tzname))
-    if tzname is not None and tzname != conn_tzname:
+    if tzname is not None and tzname == conn_tzname:
         dt = timezone.localtime(dt, pytz.timezone(tzname))
     return dt
```
**变异语义**：当 tzname == conn_tzname 时（时区相同），会多执行一次从 conn_tz 到 conn_tz 的 localtime（结果不变，因为两者相同）；当 tzname != conn_tzname 时（时区不同，需要转换），跳过 localtime，直接返回已设置 conn_tzname tzinfo 的 datetime，导致返回值的时区信息错误（tzinfo 是 conn_tz 而不是 tzname）。`test_query_convert_timezones` 中使用 Asia/Bangkok 连接时区的测试会失败。

## 新设计 Mutation 说明

**Group A（MySQL 转换方向反转）**：
基于对 `CONVERT_TZ(value, from_tz, to_tz)` MySQL 函数语义的深入理解。Golden patch 从 `'UTC'` 改为 `self.connection.timezone_name` 作为 from_tz，mutation 则将 `from_tz` 和 `to_tz` 参数位置互换。真实开发者在实现双向时区转换时很容易搞混参数顺序，尤其是在变量名不够直观时。此 mutation 能通过连接时区与查询时区相同的大多数测试（短路条件跳过），只在两者不同时失败。

**Group B（SQLite 参数顺序互换）**：
`_convert_tznames_to_sql` 返回的是一个元组，调用方通过解包 `*self._convert_tznames_to_sql(tzname)` 将参数传入 SQL 格式字符串。格式字符串的参数位置固定，mutation 通过互换元组顺序，使 `tzname` 和 `conn_tzname` 在 SQL 函数调用中的位置交换。这种错误非常隐蔽，因为代码逻辑看起来完全合理，只是两个名字相似的变量顺序颠倒了。

**Group C（SQLite 操作顺序）**：
`_sqlite_datetime_parse` 的两步操作（replace + localtime）有严格的前后依赖关系：必须先设置 tzinfo 才能做 localtime。颠倒顺序后，`localtime` 作用于一个 naive datetime，pytz 会将其当作 UTC 处理，而后 `replace` 会粗暴地覆盖 tzinfo，破坏时区转换的语义正确性。这模拟了开发者对 `replace` vs `astimezone` vs `localtime` 语义混淆导致的真实 bug。

**Group D（Oracle 短路逻辑取反）**：
Oracle 的 `_convert_field_to_tz` 增加了一个短路优化：相同时区不转换。取反后变为"仅在相同时才转换，不同时直接返回"，是一种常见的逻辑边界错误（"优化条件写反"）。由于 Oracle 测试通常不覆盖连接时区与查询时区不同的场景，这类 mutation 很难被现有测试检测到。

**Group E（SQLite 条件比较符取反）**：
`_sqlite_datetime_parse` 中的 `!= ` 条件是整个时区转换优化的关键：只在必要时才 localtime。改为 `==` 后，优化逻辑完全反转：在不需要时转换，在需要时跳过。与 Group D 类似，这模拟了"相同/不同"判断写反的真实错误，单行改动难以发现。
