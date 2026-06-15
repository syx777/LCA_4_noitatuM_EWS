# django__django-12209

## 问题背景

Django 3.0 引入了一个性能优化（ticket #29260）：当模型实例是"新增"（`_state.adding=True`）且 pk 字段有 `default` 时，直接执行 INSERT 而不先尝试 UPDATE，避免多余的 UPDATE 查询。

然而，该优化未考虑 **fixture 加载**（`raw=True`）的场景。`loaddata` 管理命令通过 `save_base(raw=True)` 直接调用，此时 fixture 中的显式 pk 值已存在于数据库中，应当执行 UPDATE。但优化代码无条件地将 `force_insert = True`，导致 INSERT 而非 UPDATE，最终抛出 IntegrityError（主键冲突）。

**Golden patch 修复**（commit `5779cc938a`）：在 `_save_table` 的条件中添加 `not raw and`，仅对非 raw 保存才触发 INSERT 优化；fixture 加载（raw=True）时仍走 UPDATE→INSERT 路径。

## Golden Patch 语义分析

修复前条件：
```python
if (
    not force_insert and
    self._state.adding and
    self._meta.pk.default and
    self._meta.pk.default is not NOT_PROVIDED
):
    force_insert = True
```

修复后：
```python
if (
    not raw and           # <-- 新增
    not force_insert and
    self._state.adding and
    self._meta.pk.default and
    self._meta.pk.default is not NOT_PROVIDED
):
    force_insert = True
```

核心语义：`raw=True` 表示"fixture 加载"，此时调用者期望对已存在 pk 的记录执行 UPDATE；`raw=False` 表示普通创建，有 default 的 pk 字段，该对象一定是全新的，可以安全地跳过 UPDATE。

## 调用链分析

```
loaddata 管理命令
  └─► Deserializer.save()
        └─► Model.save_base(raw=True, force_insert=False)
              └─► Model._save_table(raw=True, cls, force_insert=False, ...)
                    ├─► [condition: not raw and ...] → raw=True → False → 不强制 INSERT
                    ├─► if pk_set and not force_insert: → UPDATE 路径
                    │     └─► _do_update() → 更新已有行，返回 updated=True
                    └─► if not updated: → 跳过 INSERT（正确）
```

普通新对象创建路径：
```
Model.save() → Model.save_base(raw=False) → Model._save_table(raw=False, ...)
  → [condition: not raw(=False) and adding and pk.default...] → True → force_insert=True
  → 跳过 UPDATE → 直接 INSERT（性能优化，正确）
```

修改的函数 `_save_table` 在 `django/db/models/base.py` 中，被 `save_base` 直接调用。`save_base` 本身被 `save()` 和 `loaddata` 调用，是保存逻辑的核心中间层。`raw` 参数是区分两条路径的唯一标志。

**F2P 测试场景**：`tests/serializers/test_data.py` — `(pk_obj, 793, UUIDDefaultData, uuid_obj)`。
测试 `UUIDDefaultData`（UUID pk 带 default），通过序列化器的 `pk_obj` 辅助函数加载两次（第二次加载时行已存在），期望 UPDATE 成功而非 INSERT 冲突。

## 替换决策总览

（路径 B：新实例，跳过 Step 2，直接为 A/B/C/D/E 各设计一个 mutation）

| 组 | 策略代码 | 决策 | 原因摘要 |
|---|---|---|---|
| A | A1 | 新设计 | 将 `not raw and` 改为 `raw and`，反转了 raw/非raw 的 force_insert 行为 |
| B | B1 | 新设计 | 将 `not raw and` 换成 `not force_update and`，实为永真条件，完全还原原始 bug |
| C | C1 | 新设计 | 在 `save_base` 中添加无 raw 检查的 force_insert 覆盖，绕过 _save_table 的修复 |
| D | D1 | 新设计 | 在 `save_base` 传 `raw and not self._state.adding` 给 _save_table，污染 adding 场景的 raw 标志 |
| E | E1 | 新设计 | 保留修复 if 块，但添加 elif 分支，对 raw+adding+pk_set 场景重新启用 force_insert |

## 各组 Mutation 分析

### Group A — A1（条件逻辑反转）

**原 mutation**：N/A（路径 B，全新设计）

**分类**：A1 — Invert Guard Condition

**最终 mutation**：
```diff
diff --git a/django/db/models/base.py b/django/db/models/base.py
index 844c01e95e..9317923b55 100644
--- a/django/db/models/base.py
+++ b/django/db/models/base.py
@@ -849,7 +849,7 @@ class Model(metaclass=ModelBase):
         updated = False
         # Skip an UPDATE when adding an instance and primary key has a default.
         if (
-            not raw and
+            raw and
             not force_insert and
             self._state.adding and
             self._meta.pk.default and
```

**变异语义**：将守卫条件从"非 fixture 加载时才跳过 UPDATE"改为"在 fixture 加载时才跳过 UPDATE"（逻辑完全相反）。普通新对象创建（raw=False）不再强制 INSERT，而是先尝试 UPDATE（0 行更新）再 INSERT，功能正确但多一次查询。Fixture 加载（raw=True）则强制 INSERT，遇到已有 pk 时抛 IntegrityError。F2P 测试（fixture 加载含 uuid default pk 的模型）失败，P2P 测试通过（功能正确，多一次无效 UPDATE）。难以发现：代码看起来是"在 raw 场景做特殊处理"，开发者会误以为 `raw and` 是专门为 fixture 加载做的插入优化。

---

### Group B — B1（替换守卫变量）

**原 mutation**：N/A（路径 B，全新设计）

**分类**：B1 — Wrong Guard Variable

**最终 mutation**：
```diff
diff --git a/django/db/models/base.py b/django/db/models/base.py
index 844c01e95e..ba72e3f415 100644
--- a/django/db/models/base.py
+++ b/django/db/models/base.py
@@ -849,7 +849,7 @@ class Model(metaclass=ModelBase):
         updated = False
         # Skip an UPDATE when adding an instance and primary key has a default.
         if (
-            not raw and
+            not force_update and
             not force_insert and
             self._state.adding and
             self._meta.pk.default and
```

**变异语义**：将 `raw` 守卫替换为 `force_update`。对于 fixture 加载（raw=True, force_update=False），`not force_update=True`，条件仍为 True → force_insert=True → INSERT → IntegrityError。开发者可能混淆了 `force_update`（主动要求 UPDATE）和 `raw`（fixture 加载标志）两个不同的"非强制"语义，且 `force_update` 在 `_state.adding=True` 时几乎永远为 False，完全无保护效果。F2P 测试失败，P2P 测试通过（正常 add 路径 force_update=False，行为同修复前）。

---

### Group C — C1（跨函数位置错误，save_base 层）

**原 mutation**：N/A（路径 B，全新设计）

**分类**：C1 — Multi-function, Wrong Location

**最终 mutation**：
```diff
diff --git a/django/db/models/base.py b/django/db/models/base.py
index 844c01e95e..6583e4d713 100644
--- a/django/db/models/base.py
+++ b/django/db/models/base.py
@@ -780,6 +780,12 @@ class Model(metaclass=ModelBase):
             parent_inserted = False
             if not raw:
                 parent_inserted = self._save_parents(cls, using, update_fields)
+            if (
+                self._state.adding and
+                self._meta.pk.default and
+                self._meta.pk.default is not NOT_PROVIDED
+            ):
+                force_insert = True
             updated = self._save_table(
                 raw, cls, force_insert or parent_inserted,
                 force_update, using, update_fields,
```

**变异语义**：在 `save_base` 中、调用 `_save_table` 前添加一段无 `not raw` 守卫的 force_insert 覆盖逻辑。这段代码绕过了 `_save_table` 中正确修复的条件：当 fixture 加载（raw=True）时，save_base 已将 force_insert 设为 True，_save_table 收到 force_insert=True，其内部的 `not force_insert and ...` 条件为 False，但 force_insert 已经是 True，仍然强制 INSERT → IntegrityError。难以发现：看起来是"在上层也做了优化"，是对 _save_table 中修复的"配合"，而非覆盖。F2P 测试失败，P2P 测试通过。

---

### Group D — D1（raw 参数传递污染，save_base 层）

**原 mutation**：N/A（路径 B，全新设计）

**分类**：D1 — Multi-function, Parameter Corruption

**最终 mutation**：
```diff
diff --git a/django/db/models/base.py b/django/db/models/base.py
index 844c01e95e..361e459f34 100644
--- a/django/db/models/base.py
+++ b/django/db/models/base.py
@@ -781,7 +781,7 @@ class Model(metaclass=ModelBase):
             if not raw:
                 parent_inserted = self._save_parents(cls, using, update_fields)
             updated = self._save_table(
-                raw, cls, force_insert or parent_inserted,
+                raw and not self._state.adding, cls, force_insert or parent_inserted,
                 force_update, using, update_fields,
             )
```

**变异语义**：将传给 `_save_table` 的 `raw` 参数改为 `raw and not self._state.adding`。当 raw=True 且 adding=True（fixture 加载新对象）时，该表达式为 False，_save_table 收到 raw=False。然后 _save_table 中的条件 `not raw(=False) and adding(=True) and pk.default...` 变为 True → force_insert=True → INSERT → F2P 测试失败。看起来像"防止 fixture 加载时 raw 标志影响 adding 逻辑"，实际上破坏了 _save_table 中依赖 raw=True 跳过 force_insert 的保护机制。F2P 测试失败，P2P 测试通过（raw=False 时 `raw and not adding` 仍为 False，与原来的 False 相同）。

---

### Group E — E1（elif 重新触发，语义漏洞）

**原 mutation**：N/A（路径 B，全新设计）

**分类**：E1 — Semantic Hole via elif

**最终 mutation**：
```diff
diff --git a/django/db/models/base.py b/django/db/models/base.py
index 844c01e95e..d04a6f8ff5 100644
--- a/django/db/models/base.py
+++ b/django/db/models/base.py
@@ -856,6 +856,14 @@ class Model(metaclass=ModelBase):
             self._meta.pk.default is not NOT_PROVIDED
         ):
             force_insert = True
+        elif (
+            not force_insert and
+            self._state.adding and
+            pk_set and
+            self._meta.pk.default and
+            self._meta.pk.default is not NOT_PROVIDED
+        ):
+            force_insert = True
         # If possible, try an UPDATE. If that doesn't update anything, do an INSERT.
         if pk_set and not force_insert:
```

**变异语义**：保留了 `not raw and` 的修复（if 分支），但添加了一个 elif 分支，当 pk_set=True（fixture 中的显式 pk）且 adding=True 且 pk.default 存在时，重新设置 force_insert=True。该 elif 的触发条件恰好就是 fixture 加载场景：raw=True 使 if 分支跳过，但 elif 分支无 raw 检查，捕获了 fixture 加载 + pk 已设置的组合。看起来是"对 pk 已设定的场景做额外处理"，仿佛是在"补全对非 uuid auto-generated pk 的支持"。实际上该 elif 正好反向破坏了修复。F2P 测试失败，P2P 测试通过（raw=False 时 if 分支已触发，elif 不会被执行）。

## 新设计 Mutation 说明

所有 5 个 mutation 均基于对 `_save_table` 和 `save_base` 调用链的深度分析，针对 `not raw and` 这一核心修复点，从不同角度模拟开发者可能犯的逻辑错误：

- **A1**：反转 `not raw` → `raw`，逻辑完全互换，难以被简单的"happy path"测试发现
- **B1**：混淆 `raw` 与 `force_update`，利用代码审查者对参数名相似性的忽视
- **C1/D1**：跨函数引入 bug，分别在 save_base 层通过添加覆盖逻辑（C1）和污染参数传递（D1）来绕过 _save_table 中的修复，代码审查需要追踪两个函数间的交互才能发现
- **E1**：以"补充分支"的形式引入语义漏洞，保留正确的 if 块但通过 elif 重新触发 bug，看起来是功能完善而非引入缺陷
