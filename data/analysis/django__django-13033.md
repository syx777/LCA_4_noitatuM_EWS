# django__django-13033

## 问题背景

当对自引用外键（self-referential FK）进行排序时，`order_by('relation__attname_id')` 形式的查询（用 `_id` 后缀直接指定 FK 的数据库列）不正确地继承了关联模型的 `Meta.ordering`，导致：
1. 生成了不必要的额外 JOIN（自引用 FK 生成两次 JOIN）
2. 排序方向错误（被目标模型的 `-pk` 默认排序干扰）

根因：`find_ordering_name()` 函数的判断条件 `getattr(field, 'attname', None) != name` 将 `field.attname`（如 `'editor_id'`）与 `name` 整体（如 `'author__editor_id'`）比较，永远不相等，使得"用户指定了 attname 时不继承排序"的保护逻辑失效。

Golden patch 将 `name` 改为 `pieces[-1]`（最后一个分段），使比较变为 `'editor_id' != 'editor_id'` → 相等 → 跳过继承，正确地只使用直接列排序。

## Golden Patch 语义分析

```python
# base_commit 状态（有 bug）：
if field.is_relation and opts.ordering and getattr(field, 'attname', None) != name and name != 'pk':
    # 继承 opts.ordering（相关模型的默认排序）

# patched 状态（修复后）：
if (
    field.is_relation and
    opts.ordering and
    getattr(field, 'attname', None) != pieces[-1] and  ← 只比较最后一段
    name != 'pk'
):
    # 继承 opts.ordering
```

修复的核心语义：
- `name` 是完整的 lookup 路径（如 `'author__editor_id'`）
- `pieces[-1]` 是最后一段（如 `'editor_id'`）
- `field.attname` 是 FK 字段的数据库列名（如 `'editor_id'`）
- 当用户指定 `author__editor_id` 时，`pieces[-1]` = `'editor_id'` = `field.attname`，说明用户明确想要该 FK 列的直接排序，不应继承关联模型的 ordering

为什么这样修复正确：
- 单段 `author_id`：`pieces[-1]='author_id'` = `attname='author_id'` → 不继承（与原来行为一致）
- 多段 `author__editor_id`：`pieces[-1]='editor_id'` = `attname='editor_id'` → 不继承（修复后新行为）
- 多段 `author__editor`：`pieces[-1]='editor'` ≠ `attname='editor_id'` → 继承 Author.ordering（正确：用户指定的是关系名，需要关联模型的默认排序）

## 调用链分析

```
QuerySet.order_by('author__editor_id')
  → SQLCompiler.get_order_by()
  → SQLCompiler.find_ordering_name('author__editor_id', opts, ...)
      name = 'author__editor_id'
      pieces = ['author', 'editor_id']
      field, targets, alias, joins, path, opts, tf = _setup_joins(pieces, opts, alias)
      # field = Author.editor FK field
      # opts = Author._meta (editor model's meta)
      # field.attname = 'editor_id'
      
      # With bug: 'editor_id' != 'author__editor_id' → True → inherits Author.ordering!
      # With fix:  'editor_id' != pieces[-1]='editor_id' → False → direct column order ✓
      
      # If inheriting: for item in opts.ordering → '-pk' → recursively find_ordering_name('-pk', ...)
      # Returns: [OrderBy(pk_col, descending=True)] → WRONG for order_by('author__editor_id')
```

`_setup_joins` 负责将 `['author', 'editor_id']` 解析为具体的字段、join 路径和 opts。对于自引用 FK，`editor` 字段指向 Author 自身，所以 `opts` 也是 `Author._meta`，其中 `opts.ordering = ('-pk',)`。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | `pieces[-1]` 改回 `name`，等同于直接还原 golden patch 的逆操作 |
| B | 缺失 | 新设计 | B 组原本缺失，设计新 mutation |
| C | 🔴 必须替换 | 替换 | 删除整个 attname 检查行，破坏 P2P（test_order_by_fk_attname 会失败） |
| D | 缺失 | 新设计 | D 组原本缺失，设计新 mutation |
| E | 🔴 必须替换 | 替换 | 与 A 完全相同 diff 且附加了人工 debug 测试文件（不自然） |

现有 A 和 E 是直接冗余（相同），C 破坏 P2P。需替换所有3个，并设计 B 和 D。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
-            getattr(field, 'attname', None) != pieces[-1] and
+            getattr(field, 'attname', None) != pieces[0] and
```

**分类**：🔴 必须替换（原 mutation 为 `pieces[-1]` → `name`，直接还原 golden patch）

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index 686e75c979..0c6c12504c 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -730,7 +730,7 @@ class SQLCompiler:
         if (
             field.is_relation and
             opts.ordering and
-            getattr(field, 'attname', None) != pieces[-1] and
+            getattr(field, 'attname', None) != pieces[0] and
             name != 'pk'
         ):
             # Firstly, avoid infinite loops.
```

**变异语义**：将比较基准从 `pieces[-1]`（最后一段）改为 `pieces[0]`（第一段）。对于单段 lookup（如 `'author_id'`），两者相同（P2P 通过）。对于多段 lookup（如 `'author__editor_id'`），`pieces[0]='author'` ≠ `field.attname='editor_id'` → 始终为 True → 继承 Author ordering → 排序方向错误（F2P 失败）。模拟了"开发者认为第一段是关键标识符"的理解错误。

---

### Group B — 替换（新设计）

**原 mutation**：（缺失，新设计）

**分类**：🟡 新设计（B3 - Invert Boolean Logic）

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index 686e75c979..53bea44cad 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -730,7 +730,7 @@ class SQLCompiler:
         if (
             field.is_relation and
             opts.ordering and
-            getattr(field, 'attname', None) != pieces[-1] and
+            (getattr(field, 'attname', None) != pieces[-1] or len(pieces) > 1) and
             name != 'pk'
         ):
             # Firstly, avoid infinite loops.
```

**变异语义**：在 attname 检查基础上加入 `or len(pieces) > 1` 条件。当 lookup 包含多段时（`__` 分隔符），无论 attname 是否匹配，都继承关联模型的排序。对于单段 `'author_id'`：`attname != pieces[-1]` 为 False 且 `len=1 not > 1` → OR 为 False → 不继承（P2P 通过）。对于多段 `'author__editor_id'`：`len=2 > 1` → OR 为 True → 继承 Author ordering → 排序错误（F2P 失败）。模拟了"多跳 FK 应该始终继承目标模型排序"的错误逻辑假设。

---

### Group C — 替换

**原 mutation**：删除了 `getattr(field, 'attname', None) != pieces[-1] and` 整行

**分类**：🔴 必须替换（破坏 P2P：test_order_by_fk_attname 中 `order_by('author_id')` 会错误继承 Author ordering）

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index 686e75c979..d4daf970f8 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -730,7 +730,7 @@ class SQLCompiler:
         if (
             field.is_relation and
             opts.ordering and
-            getattr(field, 'attname', None) != pieces[-1] and
+            name != pieces[-1] and
             name != 'pk'
         ):
             # Firstly, avoid infinite loops.
```

**变异语义**：将 `getattr(field, 'attname', None) != pieces[-1]`（比较字段的 DB 列名与最后一段）替换为 `name != pieces[-1]`（比较完整 lookup 名与最后一段）。对于单段 lookup：`name == pieces[-1]`（相同字符串）→ False → 不继承（P2P 通过）。对于多段 lookup（无论是 `'author__editor'` 还是 `'author__editor_id'`）：`name` 包含 `__` 分隔符，永远不等于 `pieces[-1]` → True → 始终继承排序 → F2P 中 `'author__editor_id'` 得到错误 descending 排序（F2P 失败）。看起来像是"用全名判断是否为直接引用"的合理想法，但对多段 lookup 永远为 True。

---

### Group D — 替换（新设计）

**原 mutation**：（缺失，新设计）

**分类**：🟡 新设计（D3 - Introduce Sequential Dependency）

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index 686e75c979..c9c680991d 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -730,7 +730,7 @@ class SQLCompiler:
         if (
             field.is_relation and
             opts.ordering and
-            getattr(field, 'attname', None) != pieces[-1] and
+            getattr(field, 'attname', None) != name.rsplit(LOOKUP_SEP, 1)[0] and
             name != 'pk'
         ):
             # Firstly, avoid infinite loops.
```

**变异语义**：将比较基准从 `pieces[-1]`（最后一段）改为 `name.rsplit(LOOKUP_SEP, 1)[0]`（从右分割一次取左边，即 `'author__editor_id'` → `'author'`）。对于单段 `'author_id'`：`rsplit('__',1)[0]='author_id'` = `attname='author_id'` → False → 不继承（P2P 通过）。对于多段 `'author__editor_id'`：`rsplit('__',1)[0]='author'` ≠ `attname='editor_id'` → True → 继承 Author ordering → 排序错误（F2P 失败）。模拟了"开发者认为应该对比 lookup 的前缀而非后缀"的错误，`rsplit` 的语法看起来很专业和谨慎，但使用 `[0]` 而非 `[-1]` 取了错误的部分。

---

### Group E — 替换

**原 mutation**：与 A 相同（`pieces[-1]` → `name`）+ 附带了一个 debug 测试文件（不自然）

**分类**：🔴 必须替换（直接冗余 + 不自然）

**最终 mutation**：
```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index 686e75c979..e20024ed5f 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -730,7 +730,7 @@ class SQLCompiler:
         if (
             field.is_relation and
             opts.ordering and
-            getattr(field, 'attname', None) != pieces[-1] and
+            getattr(field, 'attname', None) not in (pieces[0], name) and
             name != 'pk'
         ):
             # Firstly, avoid infinite loops.
```

**变异语义**：将单值比较 `!= pieces[-1]` 替换为成员检查 `not in (pieces[0], name)`。逻辑：如果 attname 与 `pieces[0]`（第一段）或 `name`（完整路径）任何一个相等，则不继承。对于单段 `'author_id'`：`attname='author_id' in ('author_id', 'author_id')` → True → `not in` = False → 不继承（P2P 通过）。对于多段 `'author__editor_id'`：`attname='editor_id'`，检查 `in ('author', 'author__editor_id')` → False → `not in` = True → 继承 Author ordering → 排序错误（F2P 失败）。看起来像是"扩展了不继承的判断条件"（同时检查字段名和完整路径），但实际上漏掉了 `pieces[-1]` 这个真正有效的检查项。

---

## 新设计 Mutation 说明

所有 5 个 mutation 都修改同一行（`find_ordering_name` 的 attname 比较条件），但使用不同的语义：
- **A**: `pieces[0]`（第一段）—— 单段 lookup 时与 pieces[-1] 相同，多段时不同
- **B**: `or len(pieces) > 1`（多段强制继承）—— 看起来是对多跳 FK 的特殊处理
- **C**: `name != pieces[-1]`（用全名代替 attname 比较）—— 单段时等价，多段时永远 True
- **D**: `name.rsplit(LOOKUP_SEP, 1)[0]`（取前缀段）—— 单段时等价，多段时取了错误部分
- **E**: `not in (pieces[0], name)`（多条件检查）—— 扩展了检查集合但漏掉了 pieces[-1]

每个 mutation 都通过了 Step 5 语法验证，P2P 关键测试（单段 `'author_id'` 不继承 FK ordering）均保持正确。
