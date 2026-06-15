# django__django-12039

## 问题背景

在 Django 中，通过 `Index(fields=['-name'], name='idx')` 创建降序索引时，生成的 `CREATE INDEX` 语句为 `("name"DESC)` —— 列名与 `DESC` 关键字之间没有空格。此外，使用 `opclasses` 时如果是升序排序（默认），生成的语句尾部会有多余空格：`("name" text_pattern_ops )`。

Golden patch 修复了 `django/db/backends/ddl_references.py` 中的两处问题：
1. `Columns.__str__`：将原有的直接字符串拼接改为带空格的格式化字符串，并增加空字符串守卫
2. `IndexColumns.__str__`：为 suffix 追加增加了空字符串守卫，避免追加空字符串时产生尾部空格

## Golden Patch 语义分析

**核心修复逻辑**：`col_suffixes` 中对应升序列的值为空字符串 `''`，对应降序列的值为 `'DESC'`。

修复前（`Columns.__str__`）：
```python
return self.quote_name(column) + self.col_suffixes[idx]
# 升序: '"name"' + '' = '"name"'  → OK
# 降序: '"name"' + 'DESC' = '"name"DESC'  → BUG: 缺少空格
```

修复后：
```python
col = self.quote_name(column)
suffix = self.col_suffixes[idx]
if suffix:          # 关键：空字符串为 falsy，跳过格式化
    col = '{} {}'.format(col, suffix)   # 关键：format 引入空格
return col
```

修复前（`IndexColumns.__str__`）：
```python
col = '{} {}'.format(col, self.col_suffixes[idx])
# 升序: col = '"name" text_pattern_ops '  → BUG: 尾部空格
# 降序: col = '"name" text_pattern_ops DESC'  → OK
```

修复后：增加 `if suffix:` 守卫，跳过空字符串，避免尾部空格。

**关键语义**：Python 中 `if suffix:` 与 `if suffix is not None:` 的区别 —— 前者将空字符串视为 falsy，后者不会。这正是两类 mutation 的核心攻击点。

## 调用链分析

```
Index.create_sql(model, schema_editor)
  └── Index.fields_orders  # 生成 (field_name, '' | 'DESC') 的元组列表
  └── col_suffixes = [order[1] for order in self.fields_orders]  # ['' | 'DESC', ...]
  └── schema_editor._create_index_sql(... col_suffixes=col_suffixes ...)
        └── schema_editor._index_columns(table, columns, col_suffixes, opclasses)
              ├── 非 PostgreSQL: Columns(table, columns, quote_name, col_suffixes=col_suffixes)
              └── PostgreSQL (有 opclasses): IndexColumns(table, columns, quote_name, col_suffixes=col_suffixes, opclasses=opclasses)
                    └── IndexColumns.__str__() → col_str() → 最终 SQL 片段
```

`Columns` 是基类，`IndexColumns` 继承 `Columns` 并重写 `__str__` 以处理 opclasses。两者的 `col_str` 内函数都需要正确处理空字符串 suffix。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原 mutations.jsonl 中仅有 Group C，其余组需新设计 |
| B | 🔴 必须替换 | 替换 | 同上，新设计 |
| C | 🔴 必须替换 | 替换 | 原 mutation 含 `if False:  # BUG` 注释，人工痕迹明显 |
| D | 🔴 必须替换 | 替换 | 同上，新设计 |
| E | 🔴 必须替换 | 替换 | 同上，新设计 |

注：原始数据中只有 Group C 一条记录，且质量不合格（含 BUG 注释），全部5组均需设计全新 mutation。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：不存在（mutations.jsonl 中无 Group A 记录）

**分类**：🔴 必须替换（新设计）

**理由**：针对 `Columns.__str__` 中的 suffix 守卫逻辑，用 `is not None` 替代 Python 惯用的 truthiness 检查。

**最终 mutation**：
```diff
diff --git a/django/db/backends/ddl_references.py b/django/db/backends/ddl_references.py
index ba55de1df8..7a33deabca 100644
--- a/django/db/backends/ddl_references.py
+++ b/django/db/backends/ddl_references.py
@@ -86,7 +86,7 @@ class Columns(TableColumns):
             col = self.quote_name(column)
             try:
                 suffix = self.col_suffixes[idx]
-                if suffix:
+                if suffix is not None:
                     col = '{} {}'.format(col, suffix)
             except IndexError:
                 pass
```

**变异语义**：`'' is not None` 为 `True`，导致升序列（suffix 为空字符串）被格式化为 `"col_name "` (尾部空格)。降序测试（suffix='DESC'）仍通过，因为 `'DESC' is not None` 也为 True 且格式化正确。测试 `test_columns_list_sql` 会检查 `("headline")` 是否在 SQL 中，实际得到 `("headline" )` 而 assertIn 失败。这个修改看起来很"保守"（is not None 是常见的 Python None 守卫写法），审查者需要意识到空字符串的 falsy 语义才能发现问题。

---

### Group B — 替换

**原 mutation**：不存在（新设计）

**分类**：🔴 必须替换（新设计）

**理由**：针对 `Columns.__str__` 中的字符串拼接方式，用直接拼接替代 format（丢失空格）。

**最终 mutation**：
```diff
diff --git a/django/db/backends/ddl_references.py b/django/db/backends/ddl_references.py
index ba55de1df8..dbc8ee3a20 100644
--- a/django/db/backends/ddl_references.py
+++ b/django/db/backends/ddl_references.py
@@ -87,7 +87,7 @@ class Columns(TableColumns):
             try:
                 suffix = self.col_suffixes[idx]
                 if suffix:
-                    col = '{} {}'.format(col, suffix)
+                    col = col + suffix
             except IndexError:
                 pass
             return col
```

**变异语义**：`col + 'DESC'` = `'"name"DESC'`（无空格），而期望是 `'"name" DESC'`。升序列（suffix=''）不受影响（`if suffix:` 为 False，不执行拼接）。测试 `test_descending_columns_list_sql` 检查 `("headline" DESC)` 是否在 SQL 中，实际得到 `("headline"DESC)` 而 assertIn 失败。直接拼接是 Python 中非常自然的字符串操作，审查者很容易忽略缺少的空格。

---

### Group C — 替换（原有 Group C 替换为更高质量版本）

**原 mutation**：
```diff
-                if suffix:
+                if False:  # BUG: removed suffix check
                     col = '{} {}'.format(col, suffix)
```

**分类**：🔴 必须替换 —— 含 `# BUG` 注释，人工痕迹极为明显，代码审查必定发现。

**理由**：`if False:` 是无意义的死代码，且注释直接说明是 bug，完全不符合自然代码风格。

**最终 mutation**：
```diff
diff --git a/django/db/backends/ddl_references.py b/django/db/backends/ddl_references.py
index ba55de1df8..a1a78505b3 100644
--- a/django/db/backends/ddl_references.py
+++ b/django/db/backends/ddl_references.py
@@ -119,7 +119,7 @@ class IndexColumns(Columns):
             col = '{} {}'.format(self.quote_name(column), self.opclasses[idx])
             try:
                 suffix = self.col_suffixes[idx]
-                if suffix:
+                if suffix is not None:
                     col = '{} {}'.format(col, suffix)
             except IndexError:
                 pass
```

**变异语义**：在 `IndexColumns.__str__` 中，`'' is not None` 为 True，导致升序+opclass 的列被格式化为 `"col opclass "` (尾部空格)。测试 `test_ops_class_columns_lists_sql` 检查 `("headline" text_pattern_ops)` 是否在 SQL 中，实际得到 `("headline" text_pattern_ops )` 而 assertIn 失败。降序+opclass（suffix='DESC'）仍然正确通过。与 Group A 互补（A 攻击 `Columns`，C 攻击 `IndexColumns`）。

---

### Group D — 替换

**原 mutation**：不存在（新设计）

**分类**：🔴 必须替换（新设计）

**理由**：针对 `IndexColumns.__str__` 中 opclass 与列名之间的空格，移除分隔符。

**最终 mutation**：
```diff
diff --git a/django/db/backends/ddl_references.py b/django/db/backends/ddl_references.py
index ba55de1df8..c0328013f6 100644
--- a/django/db/backends/ddl_references.py
+++ b/django/db/backends/ddl_references.py
@@ -116,7 +116,7 @@ class IndexColumns(Columns):
         def col_str(column, idx):
             # Index.__init__() guarantees that self.opclasses is the same
             # length as self.columns.
-            col = '{} {}'.format(self.quote_name(column), self.opclasses[idx])
+            col = '{}{}'.format(self.quote_name(column), self.opclasses[idx])
             try:
                 suffix = self.col_suffixes[idx]
                 if suffix:
```

**变异语义**：`'{}{}'.format('"headline"', 'text_pattern_ops')` = `'"headline"text_pattern_ops'`，缺少空格。非 opclass 测试（tests 1/2）不受影响，因为走 `Columns.__str__` 路径。tests 3 和 4 均失败：期望 `("headline" text_pattern_ops)` / `("headline" text_pattern_ops DESC)` 但实际得到 `("headline"text_pattern_ops)` / `("headline"text_pattern_ops DESC)`。format 字符串中缺少空格极难在快速审查中发现。

---

### Group E — 替换

**原 mutation**：不存在（新设计）

**分类**：🔴 必须替换（新设计）

**理由**：在 `IndexColumns.__str__` 中交换了 suffix 和 opclass 的拼接顺序，模拟开发者对 SQL 语法的误解。

**最终 mutation**：
```diff
diff --git a/django/db/backends/ddl_references.py b/django/db/backends/ddl_references.py
index ba55de1df8..fa93930a2c 100644
--- a/django/db/backends/ddl_references.py
+++ b/django/db/backends/ddl_references.py
@@ -116,13 +116,14 @@ class IndexColumns(Columns):
         def col_str(column, idx):
             # Index.__init__() guarantees that self.opclasses is the same
             # length as self.columns.
-            col = '{} {}'.format(self.quote_name(column), self.opclasses[idx])
+            col = self.quote_name(column)
             try:
                 suffix = self.col_suffixes[idx]
                 if suffix:
                     col = '{} {}'.format(col, suffix)
             except IndexError:
                 pass
+            col = '{} {}'.format(col, self.opclasses[idx])
             return col
```

**变异语义**：原逻辑是先拼接 `column opclass`，再追加 `DESC`，得到 `column opclass DESC`。变异后先拼接 `column DESC`，再追加 `opclass`，得到 `column DESC opclass`。升序（suffix=''）时结果仍为 `column opclass`（`if suffix:` 不触发），test 3 通过。降序（suffix='DESC'）时结果为 `column DESC text_pattern_ops`，期望为 `column text_pattern_ops DESC`，test 4 失败。这个 mutation 模拟了开发者误认为 "排序方向应先于 opclass 修饰符" 的错误，在不了解 PostgreSQL opclass 语法的审查者看来完全合理。

## 新设计 Mutation 说明

### 设计原则
所有5个 mutation 均位于 `django/db/backends/ddl_references.py`，覆盖两个相关类（`Columns` 和 `IndexColumns`）的不同逻辑点：

1. **Group A**（`Columns`, suffix truthiness）：利用 Python `is not None` 与 truthiness 的细微差别，针对升序列的空字符串 suffix。
2. **Group B**（`Columns`, format vs concat）：复现原始 bug 的本质（直接字符串拼接无空格），修改最小，但语义错误明确。
3. **Group C**（`IndexColumns`, suffix truthiness）：与 Group A 同类型但在不同类中，针对 PostgreSQL opclass 场景的升序列。
4. **Group D**（`IndexColumns`, opclass separator）：攻击 opclass 与列名之间的分隔符，是格式字符串中单字符变化（`'%s %s'` → `'%s%s'`），极难发现。
5. **Group E**（`IndexColumns`, opclass/suffix ordering）：多行重构型 mutation，改变了 opclass 和 suffix 的拼接顺序，语义层面模拟了对 `CREATE INDEX ... opclass DESC` 语法的误解。

所有 mutation 通过了 `patch --dry-run` 验证（可正确应用）和 `py_compile` 语法验证。
