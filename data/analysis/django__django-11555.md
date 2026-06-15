# django__django-11555

## 问题背景

使用多表继承（Multi-Table Inheritance）时，若父模型的 `Meta.ordering` 中包含表达式（如 `models.F('author').asc(nulls_first=True)`），则对子模型调用 `order_by()` 会触发崩溃。根本原因是 `find_ordering_name` 在遍历 `opts.ordering` 时，将 `OrderBy` 对象直接传入自身递归调用，而该方法内部第一步调用 `get_order_dir(name, default_order)` 会执行 `name[0]`（字符串下标访问），对 `OrderBy` 对象执行此操作会抛出 `TypeError`。

Golden patch 的修复：在遍历 `opts.ordering` 的循环中，检测 `item` 是否为 `OrderBy` 实例，若是则直接 `append((item, False))` 并 `continue`，跳过递归处理。

## Golden Patch 语义分析

修复的核心逻辑：`Meta.ordering` 中的字符串条目（如 `'id'`）需要经过 `find_ordering_name` 解析为 `(OrderBy, is_ref)` 元组；而已经是 `OrderBy` 对象的条目（如 `models.F('author').asc(nulls_first=True)`）已完成解析，无需再次处理，直接保留其原有方向和参数（`nulls_first` 等）即可。错误在于没有区分"已解析的表达式"与"待解析的字符串"这两类 `opts.ordering` 条目。

## 调用链分析

```
ChildArticle.objects.order_by('article_ptr')
  └→ SQLCompiler.get_order_by()
       └→ SQLCompiler.find_ordering_name('article_ptr', ChildArticle_opts, ...)
            ├── get_order_dir('article_ptr', 'ASC') → ('article_ptr', 'ASC')
            ├── _setup_joins(['article_ptr'], ...)
            │   → field = article_ptr (OneToOneField, is_relation=True)
            │   → opts = Article._meta (包含 Meta.ordering)
            ├── 进入 if field.is_relation and opts.ordering and attname != name 分支
            └── for item in Article.Meta.ordering:
                    item1 = OrderBy(F('author'), nulls_first=True)  ← 需要 golden patch 处理
                    item2 = 'id'
                        └→ find_ordering_name('id', Article_opts, ...) → [(OrderBy(id_col), False)]
```

`get_order_dir(name, default)` 实现为 `name[0] == '-'`，若 `name` 是 `OrderBy` 对象则崩溃。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失 | 新增 | mutations.jsonl 中不存在 Group A，必须新建 |
| B | 🔴 必须替换 | 替换 | 与 C、D 完全相同的 diff，是 golden patch 的直接逆操作 |
| C | 🔴 必须替换 | 替换 | 与 B、D 完全相同的 diff，直接冗余 |
| D | 🔴 必须替换 | 替换 | 与 B、C 完全相同的 diff，直接冗余 |
| E | 🔴 必须替换 | 替换 | `isinstance(item, Value)` 在此上下文永远为 False，功能等价于删除 check |

语义浅层共 0 个（所有原始 mutation 均为必须替换级别）。

---

## 各组 Mutation 分析

### Group A — 替换（新增）

**原 mutation**：无（mutations.jsonl 中缺失）

**分类**：缺失，必须新增

**最终 mutation**：

```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index 3cab405d2d..4983267de1 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -712,7 +712,7 @@ class SQLCompiler:
         # If we get to this point and the field is a relation to another model,
         # append the default ordering for that model unless the attribute name
         # of the field is specified.
-        if field.is_relation and opts.ordering and getattr(field, 'attname', None) != name:
+        if field.is_relation and opts.ordering and getattr(field, 'attname', None) == name:
             # Firstly, avoid infinite loops.
             already_seen = already_seen or set()
             join_tuple = tuple(getattr(self.query.alias_map[j], 'join_cols', None) for j in joins)
```

**变异语义**：将条件 `attname != name` 改为 `attname == name`，颠倒了"是否跟随父模型排序"的判断逻辑。对于 `article_ptr` 字段，`attname='article_ptr_id'` 与 `name='article_ptr'` 不相等，原始条件为 True（进入父模型排序分支），变异后为 False（跳过分支，直接返回 `article_ptr_id` 列排序）。这是一个典型的条件边界失误，代码审查时很难发现，因为注释说的是"除非 attname == name 时才跳过"，而实际条件读起来自然。

---

### Group B — 替换

**原 mutation**：

```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index 3cab405d2d..eaccc96d7d 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -722,9 +722,6 @@ class SQLCompiler:
 
             results = []
             for item in opts.ordering:
-                if isinstance(item, OrderBy):
-                    results.append((item, False))
-                    continue
                 results.extend(self.find_ordering_name(item, opts, alias,
                                                        order, already_seen))
             return results
```

**分类**：🔴 必须替换（直接逆操作 golden patch，与 C/D 完全相同）

**最终 mutation**：

```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index 3cab405d2d..b2469bb789 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -724,7 +724,6 @@ class SQLCompiler:
             for item in opts.ordering:
                 if isinstance(item, OrderBy):
                     results.append((item, False))
-                    continue
                 results.extend(self.find_ordering_name(item, opts, alias,
                                                        order, already_seen))
             return results
```

**变异语义**：保留 `isinstance` 检测和 `append`，但删去 `continue`，导致对 `OrderBy` 对象执行完 append 后，仍然落入下方的 `find_ordering_name(item, ...)` 调用。`find_ordering_name` 首先调用 `get_order_dir(item, ...)` → `item[0]`，对 `OrderBy` 对象执行下标访问会引发 `TypeError`。模拟了开发者"忘记加 continue"这一常见失误，代码审查时因为检测逻辑本身看起来完整而难以察觉。

---

### Group C — 替换

**原 mutation**：（与 B 完全相同，略）

**分类**：🔴 必须替换

**最终 mutation**：

```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index 3cab405d2d..8de8a91439 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -723,7 +723,6 @@ class SQLCompiler:
             results = []
             for item in opts.ordering:
                 if isinstance(item, OrderBy):
-                    results.append((item, False))
                     continue
                 results.extend(self.find_ordering_name(item, opts, alias,
                                                        order, already_seen))
```

**变异语义**：保留 `isinstance` 检测和 `continue`，但删去 `results.append((item, False))`。效果是 `OrderBy` 类型的 meta ordering 条目（如 `F('author').asc(nulls_first=True)`）被静默丢弃，只保留字符串条目（如 `'id'`）的排序。F2P 测试期望的顺序依赖 `nulls_first=True` 的 author 表达式排序，丢弃后顺序出错。这模拟了开发者误以为 `isinstance` 块只是"跳过"而不需要 append 的逻辑误解。

---

### Group D — 替换

**原 mutation**：（与 B、C 完全相同，略）

**分类**：🔴 必须替换

**最终 mutation**：

```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index 3cab405d2d..51dad10f37 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -725,7 +725,7 @@ class SQLCompiler:
                 if isinstance(item, OrderBy):
                     results.append((item, False))
                     continue
-                results.extend(self.find_ordering_name(item, opts, alias,
+                results.append(self.find_ordering_name(item, opts, alias,
                                                        order, already_seen))
             return results
         targets, alias, _ = self.query.trim_joins(targets, joins, path)
```

**变异语义**：将 `results.extend(...)` 改为 `results.append(...)`。`find_ordering_name` 返回一个列表（如 `[(OrderBy(id_col), False)]`），`extend` 会展开并追加每个元素，而 `append` 会将整个列表作为单个元素追加，使 `results` 变为 `[(OrderBy(author), False), [(OrderBy(id_col), False)]]`。后续 `get_order_by` 对此列表做 `for expr, is_ref in order_by` 解包时，第二项是列表而非二元组，导致解包失败（`ValueError: not enough values to unpack`）。这是 Python 中 `extend` vs `append` 的经典混淆错误，对应 group D 中多行影响的策略。

---

### Group E — 替换

**原 mutation**：

```diff
-                if isinstance(item, OrderBy):
+                if isinstance(item, Value):
```

**分类**：🔴 必须替换（`Value` 不出现在 `opts.ordering` 中，条件永远为 False，功能等价于删除整个 check block）

**最终 mutation**：

```diff
diff --git a/django/db/models/sql/compiler.py b/django/db/models/sql/compiler.py
index 3cab405d2d..e677518a3d 100644
--- a/django/db/models/sql/compiler.py
+++ b/django/db/models/sql/compiler.py
@@ -725,7 +725,7 @@ class SQLCompiler:
                 if isinstance(item, OrderBy):
                     results.append((item, False))
                     continue
-                results.extend(self.find_ordering_name(item, opts, alias,
+                results.extend(self.find_ordering_name(item, opts, None,
                                                        order, already_seen))
             return results
         targets, alias, _ = self.query.trim_joins(targets, joins, path)
```

**变异语义**：在递归调用 `find_ordering_name` 时，将 `alias` 改为 `None`。正确行为是传入当前 JOIN 的 alias，以确保递归解析在同一 JOIN 上下文中进行。传入 `None` 时，`_setup_joins` 内部会调用 `self.query.get_initial_alias()` 重新分配 alias，可能将字符串类型的 ordering 条目（如 `'id'`）解析到错误的表（如 `ordering_childarticle` 而非 `ordering_article`），导致 SQL 引用错误列或表，从而产生错误的排序结果。这模拟了开发者以为 alias 参数可以重新初始化的错误假设。

---

## 新设计 Mutation 说明

**Group A**：基于对 `find_ordering_name` 中 `attname` 与 `name` 比较逻辑的深度分析。`attname` 是 ORM 字段在数据库层的列名（对 FK/O2O 字段有 `_id` 后缀），而 `name` 是用户传入的字段路径名。这两者在正常情况下几乎从不相等，所以将 `!=` 改为 `==` 是一个极难被发现的条件失误——它让整个父模型排序功能在绝大多数场景下都静默失效。

**Group B**：`continue` 在 Python 循环控制中的遗漏是高频真实错误，开发者在添加条件处理块时可能忘记跳过后续默认分支，尤其当两个分支的代码看似都"有意义"时。

**Group C**：开发者可能误以为 `isinstance` 块的作用是"标记该 item 应跳过字符串处理"，而忘记在跳过之前必须先将其追加到结果中。Silent drop 类错误（不崩溃但丢失数据）比崩溃类错误更难在测试中被发现。

**Group D**：`extend` vs `append` 是 Python 中最常见的集合操作混淆之一，在调用返回列表的函数时尤其容易发生。此 mutation 不影响 `OrderBy` 类型的项（因为它们走 `append` 分支），只影响字符串类型的 ordering 项，使错误仅在有字符串 ordering 条目时暴露。

**Group E**：基于对 `_setup_joins` 和 alias 传播机制的理解。`alias` 参数决定了 JOIN 链的起始点；传 `None` 会触发 `get_initial_alias()` 重置，在多表继承场景下导致列引用指向子表而非父表。这是一个深层次的别名传播错误，只在多表继承且父模型有 ordering 时才暴露。
