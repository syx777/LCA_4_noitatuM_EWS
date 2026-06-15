# django__django-13089

## 问题背景

`cache.backends.db.DatabaseCache._cull()` 方法在缓存条目极少（或近空）时会偶发性地抛出 `TypeError: 'NoneType' object is not subscriptable`。原因：

```python
# base_commit 状态（有 bug）：
cursor.execute(connection.ops.cache_key_culling_sql() % table, [cull_num])
cursor.execute("DELETE FROM %s WHERE cache_key < %%s" % table, [cursor.fetchone()[0]])
```

`cache_key_culling_sql()` 返回 `SELECT cache_key ... LIMIT 1 OFFSET %s`，当缓存表为空或 `cull_num=0` 且表为空时，`cursor.fetchone()` 返回 `None`，直接 `None[0]` 引发 `TypeError`。

Golden patch 将结果存入变量，并加 `if last_cache_key:` 保护：
```python
last_cache_key = cursor.fetchone()
if last_cache_key:
    cursor.execute('DELETE FROM %s WHERE cache_key < %%s' % table, [last_cache_key[0]])
```

## Golden Patch 语义分析

修复的核心逻辑：DB API `cursor.fetchone()` 在无结果时返回 `None`（而非空元组）。原代码直接对返回值下标访问，假设了结果始终存在。

修复的两个关键步骤：
1. **先保存结果**：将 `fetchone()` 结果赋给 `last_cache_key`
2. **加 None 检查**：`if last_cache_key:` — Python 中 `None` 为 falsy，空元组 `()` 也为 falsy，非空元组为 truthy；此条件仅在有有效行时执行 DELETE

F2P 测试场景（`test_cull_delete_when_store_empty`）：
- 设置 `_max_entries = -1`（所有非负 COUNT 都 > -1，强制触发 cull）
- 调用 `set()` 时：`SELECT COUNT(*) = 0`，`0 > -1` → 调用 `_cull`
- 在 `_cull` 中：DELETE expired → 表仍空 → `COUNT = 0`，`cull_num = 0 // 3 = 0`
- `OFFSET 0` 在空表上 → `fetchone()` 返回 `None`
- 修复前：`None[0]` → `TypeError` → `set()` 崩溃 → key 未存储 → `has_key` 返回 `False`
- 修复后：`if None:` → `False` → 跳过 DELETE → set 正常完成 → `has_key` 返回 `True`

## 调用链分析

```
caches['cull'].set('force_cull_delete', 'value', 1000)
  → DatabaseCache.set() → DatabaseCache._base_set()
      SELECT COUNT(*) → num = 0
      0 > -1 (max_entries = -1) → True
      → DatabaseCache._cull(db, cursor, now)
          DELETE WHERE expires < now → 0 rows affected
          SELECT COUNT(*) → 0
          0 > -1 → True
          cull_num = 0 // 3 = 0
          cursor.execute(cache_key_culling_sql % table, [0])
          # SELECT cache_key FROM table ORDER BY cache_key LIMIT 1 OFFSET 0
          # Empty table → no row → fetchone() = None
          last_cache_key = None
          if None: → False → skip DELETE ← 修复保护
      ← _cull 正常返回
      INSERT/UPDATE cache entry → key 'force_cull_delete' stored
  ← set 正常返回
has_key('force_cull_delete') → True ✓
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失 | 新设计 | A 组缺失，需全新设计 |
| B | 缺失 | 新设计 | B 组缺失，需全新设计 |
| C | 🔴 必须替换 | 替换 | 与 D/E 相同 diff，直接冗余 |
| D | 🔴 必须替换 | 替换 | 与 C/E 相同 diff，直接冗余 |
| E | 🔴 必须替换 | 替换 | 与 C/D 相同 diff，直接冗余 |

所有 3 个现有 mutation 完全相同（移除 `if last_cache_key:` 守卫），均为直接还原 golden patch。全部替换。

## 各组 Mutation 分析

### Group A — 替换（新设计）

**原 mutation**：（缺失）

**分类**：A1（修改参数语义：改变 None 检测的方式）

**最终 mutation**：
```diff
diff --git a/django/core/cache/backends/db.py b/django/core/cache/backends/db.py
index acbe702255..fa2f116fee 100644
--- a/django/core/cache/backends/db.py
+++ b/django/core/cache/backends/db.py
@@ -268,7 +268,7 @@ class DatabaseCache(BaseDatabaseCache):
                     connection.ops.cache_key_culling_sql() % table,
                     [cull_num])
                 last_cache_key = cursor.fetchone()
-                if last_cache_key:
+                if len(last_cache_key) > 0:
                     cursor.execute(
                         'DELETE FROM %s WHERE cache_key < %%s' % table,
                         [last_cache_key[0]],
```

**变异语义**：将隐式布尔检查 `if last_cache_key:` 改为显式长度检查 `if len(last_cache_key) > 0`，看起来更明确。但 `len(None)` 会引发 `TypeError: object of type 'NoneType' has no len()`，在 `fetchone()` 返回 `None` 时崩溃。正常 cull（fetchone 返回 `('some_key',)`）：`len(...)=1>0` → True → DELETE 正常执行（P2P 通过）。难以发现因为 `len()>0` 看起来比布尔隐式转换更"安全"和"显式"。

---

### Group B — 替换（新设计）

**原 mutation**：（缺失）

**分类**：B2（移除空值处理：用 try/except 捕获错误异常类型）

**最终 mutation**：
```diff
diff --git a/django/core/cache/backends/db.py b/django/core/cache/backends/db.py
index acbe702255..15bd875c2f 100644
--- a/django/core/cache/backends/db.py
+++ b/django/core/cache/backends/db.py
@@ -268,11 +268,13 @@ class DatabaseCache(BaseDatabaseCache):
                     connection.ops.cache_key_culling_sql() % table,
                     [cull_num])
                 last_cache_key = cursor.fetchone()
-                if last_cache_key:
+                try:
                     cursor.execute(
                         'DELETE FROM %s WHERE cache_key < %%s' % table,
                         [last_cache_key[0]],
                     )
+                except KeyError:
+                    pass
 
     def clear(self):
         db = router.db_for_write(self.cache_model_class)
```

**变异语义**：将 `if last_cache_key:` 守卫替换为 `try/except KeyError: pass`，看起来像是 EAFP 风格的错误处理。但 `None[0]` 引发的是 `TypeError`，不是 `KeyError`，因此 except 不捕获实际异常，`TypeError` 继续传播 → F2P 崩溃。正常 cull（fetchone 返回有效元组）：无异常抛出 → P2P 通过。难以发现：try/except 看起来比 if 更健壮，且 `KeyError` 是 DB 操作中常见的异常类型，但选错了异常类。

---

### Group C — 替换

**原 mutation**（来自 mutations.jsonl）：移除 `if last_cache_key:` 守卫（直接冗余）

**分类**：🔴 必须替换

**最终 mutation**：
```diff
diff --git a/django/core/cache/backends/db.py b/django/core/cache/backends/db.py
index acbe702255..20f64e074a 100644
--- a/django/core/cache/backends/db.py
+++ b/django/core/cache/backends/db.py
@@ -268,7 +268,7 @@ class DatabaseCache(BaseDatabaseCache):
                     connection.ops.cache_key_culling_sql() % table,
                     [cull_num])
                 last_cache_key = cursor.fetchone()
-                if last_cache_key:
+                if last_cache_key != ():
                     cursor.execute(
                         'DELETE FROM %s WHERE cache_key < %%s' % table,
                         [last_cache_key[0]],
```

**变异语义**：将 `if last_cache_key:` 改为 `if last_cache_key != ():`，看起来像是"明确检查非空元组"。但 `None != ()` 在 Python 中是 `True`（不同类型的不等比较），因此 `None` 会通过此检查，然后 `None[0]` 引发 `TypeError`。正常 cull：`('some_key',) != ()` → True → DELETE 正常（P2P 通过）。难以发现：开发者可能认为 fetchone 要么返回 tuple 要么返回空 tuple，但实际返回 `None`。

---

### Group D — 替换

**原 mutation**（来自 mutations.jsonl）：移除 `if last_cache_key:` 守卫（直接冗余，与 C/E 相同）

**分类**：🔴 必须替换

**最终 mutation**：
```diff
diff --git a/django/core/cache/backends/db.py b/django/core/cache/backends/db.py
index acbe702255..200de2e283 100644
--- a/django/core/cache/backends/db.py
+++ b/django/core/cache/backends/db.py
@@ -268,7 +268,7 @@ class DatabaseCache(BaseDatabaseCache):
                     connection.ops.cache_key_culling_sql() % table,
                     [cull_num])
                 last_cache_key = cursor.fetchone()
-                if last_cache_key:
+                if last_cache_key is not False:
                     cursor.execute(
                         'DELETE FROM %s WHERE cache_key < %%s' % table,
                         [last_cache_key[0]],
```

**变异语义**：将隐式布尔检查改为显式同一性检查 `is not False`，看起来像是"明确排除错误状态，而非使用模糊的布尔转换"。但 `None is not False` 是 `True`（None 和 False 是不同对象），因此 None 会通过守卫，然后 `None[0]` → `TypeError` → F2P 崩溃。正常 cull：`('key',) is not False` → True → DELETE 正常（P2P 通过）。难以发现：`is not False` 比 `truthy` 检查看起来更精确，但 Python 的三值语义（None/False/truthy）使这个"改进"实际上扩大了条件通过范围。

---

### Group E — 替换

**原 mutation**（来自 mutations.jsonl）：移除 `if last_cache_key:` 守卫（直接冗余，与 C/D 相同）

**分类**：🔴 必须替换

**最终 mutation**：
```diff
diff --git a/django/core/cache/backends/db.py b/django/core/cache/backends/db.py
index acbe702255..f760fa9ff1 100644
--- a/django/core/cache/backends/db.py
+++ b/django/core/cache/backends/db.py
@@ -268,7 +268,7 @@ class DatabaseCache(BaseDatabaseCache):
                     connection.ops.cache_key_culling_sql() % table,
                     [cull_num])
                 last_cache_key = cursor.fetchone()
-                if last_cache_key:
+                if last_cache_key[0]:
                     cursor.execute(
                         'DELETE FROM %s WHERE cache_key < %%s' % table,
                         [last_cache_key[0]],
```

**变异语义**：将容器级别的 None 检查（`if last_cache_key:`）改为直接对元组第一个元素的检查（`if last_cache_key[0]:`），看起来像是"直接验证实际的 cache_key 值是否有效"。当 `last_cache_key=None` 时，`None[0]` 立即引发 `TypeError`（不同于 C 的先通过检查再崩溃）。正常 cull：`('cull10',)[0] = 'cull10'` 是 truthy → DELETE 正常（P2P 通过）。难以发现：对元素而非容器的直接检查看起来更精确，但在 None 情况下不安全。

---

## 新设计 Mutation 说明

所有 5 个 mutation 都针对同一行（`if last_cache_key:`），但通过不同方式在 `fetchone()` 返回 `None` 时引发错误：

| 组 | 错误类型 | 触发方式 |
|---|---|---|
| A | TypeError | `len(None)` |
| B | TypeError（未捕获） | `None[0]`，但 except 捕获 KeyError |
| C | TypeError | `None != ()` 为 True → `None[0]` |
| D | TypeError | `None is not False` 为 True → `None[0]` |
| E | TypeError | `None[0]` 直接访问（先检查元素再检查容器） |

P2P 安全性：正常 cull 时 `fetchone()` 返回有效元组，所有 5 个 mutation 的条件都返回 True，DELETE 正常执行，`test_cull` 和 `test_zero_cull` 通过。
