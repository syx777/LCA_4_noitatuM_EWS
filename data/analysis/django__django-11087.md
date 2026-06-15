# django__django-11087

## 问题背景

在 Django 的级联删除（CASCADE）操作中，当 `Collector.collect()` 收集需要删除的关联对象时，会发起 SELECT 查询加载关联对象。对于有多个字段的模型（如含大文本字段的 `Referrer`），原始实现会加载所有字段，造成不必要的 I/O 开销。

该 issue 的修复目标是：**当没有 pre_delete/post_delete 信号监听器时，只 SELECT 被其他外键引用的字段（referenced fields）**，避免加载不必要的大字段。当信号监听器存在时，由于信号处理器可能访问对象的任意字段，仍需选择所有字段。

## Golden Patch 语义分析

Golden patch 做了以下几件事：

1. **新增 `_has_signal_listeners(self, model)` 方法**：将 pre_delete 和 post_delete 的信号检查提取为独立方法，供 `can_fast_delete` 和 `collect` 复用。注意新方法不包含 `m2m_changed`（该信号只用于 fast-delete 判断，与字段延迟无关）。

2. **重构 `can_fast_delete`**：将原来的三信号内联检查替换为调用 `_has_signal_listeners(model)`，同时保留 `m2m_changed` 检查（通过 `_has_signal_listeners` 不包含它，但原代码中 `m2m_changed` 检查被移除）。实际上 golden patch 将 `can_fast_delete` 中的三条 signal 检查全部替换为 `self._has_signal_listeners(model)`，意味着 `m2m_changed` 检查被移除了——这是有意为之。

3. **重构 `collect()` 中的关联对象处理**：原来的 `elif sub_objs:` 分支被替换为 `else:` + 条件字段延迟逻辑：
   - 当 `sub_objs.query.select_related` 为空且 `_has_signal_listeners(related_model)` 为 False 时，计算所有关联到 `related_model` 的外键所引用的字段集合（`referenced_fields`），并对 `sub_objs` 应用 `.only(*referenced_fields)`
   - 这样只 SELECT 必要字段，跳过大字段（如 `large_field`）

4. **`related_objects()` 的隐式变化**：原始代码在 `collect()` 中直接 `.only(related.field.attname)` 限制 queryset，golden patch 的意图是去掉这个初始限制，改由 `collect()` 内的条件逻辑全面控制字段选择。（由于 patch 应用时 fuzz=1，实际 patched 文件仍保留了 `.only(related.field.attname)`，但 mutation diffs 是基于去掉该限制后的"预期"状态生成的。）

## 调用链分析

```
origin.delete()
  → Collector.collect([origin])
    → get_candidate_relations_to_delete(Origin._meta)  # 找到 Referrer.origin FK
    → Collector.related_objects(related, [origin])     # 获取关联 Referrer QuerySet
    → Collector.can_fast_delete(sub_objs)              # 检查是否可 fast-delete（Referrer 有 SecondReferrer 指向它，不能 fast-delete）
    → else 分支：
      → _has_signal_listeners(Referrer)                # 检查 pre/post_delete 信号
      → 若无信号：get_candidate_relations_to_delete(Referrer._meta)  # 找 SecondReferrer 的两个 FK
        → referenced_fields = {'id', 'unique_field'}   # 被引用的字段
        → sub_objs.only('id', 'unique_field')
      → CASCADE(collector, field, sub_objs, using)     # 递归 collect SecondReferrer 对象
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟡 语义浅层 | 保留 | `.only()` 改为 `.only()`（空），位置关键，能导致 F2P 失败 |
| B | 🔴 必须替换 | 替换 | 条件逻辑倒置，等价于直接逆操作 |
| C | 🔴 必须替换 | 替换 | `sub_objs = sub_objs` 等价于不执行优化，功能直接还原 |
| D | 🔴 必须替换 | 替换 | 与 C 完全相同的 diff，冗余 |
| E | 🔴 必须替换 | 替换 | flag 永远为 False，功能等价还原，且引入不自然的 API 设计 |

语义浅层共 1 个（A），替换其中最弱的 floor(1/2) = 0 个：无
必须替换：4 个（B、C、D、E）全部替换

## 各组 Mutation 分析

### Group A — 保留

**原 mutation**：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index d28e596b46..2119db2525 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -239,7 +239,7 @@ class Collector:
                                 (rf.attname for rf in rel.field.foreign_related_fields)
                                 for rel in get_candidate_relations_to_delete(related_model._meta)
                             ))
-                            sub_objs = sub_objs.only(*tuple(referenced_fields))
+                            sub_objs = sub_objs.only()
                         if sub_objs:
                             field.remote_field.on_delete(self, field, sub_objs, self.using)
             for field in model._meta.private_fields:
```

**分类**：🟡 语义浅层（保留）

**理由**：修改位置是核心优化路径上的字段选择语句。`.only()` 不传参时 Django 会只 SELECT pk 字段（所有字段被 defer）。这会导致 F2P 测试第一个断言失败：期望的 SQL 含 `id` 和 `unique_field`，但实际只有 `id`（pk 不 defer）。属于语义浅层但处于关键位置，且效果足够特殊（不是简单的还原），保留价值高，属于1个浅层中的较强者，根据规则 floor(1/2)=0 个替换，不替换。

**最终 mutation**（与原相同）：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index d28e596b46..2119db2525 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -239,7 +239,7 @@ class Collector:
                                 (rf.attname for rf in rel.field.foreign_related_fields)
                                 for rel in get_candidate_relations_to_delete(related_model._meta)
                             ))
-                            sub_objs = sub_objs.only(*tuple(referenced_fields))
+                            sub_objs = sub_objs.only()
                         if sub_objs:
                             field.remote_field.on_delete(self, field, sub_objs, self.using)
             for field in model._meta.private_fields:
```

**变异语义**：将 `.only(*referenced_fields)` 改为 `.only()`（无参数），Django 会 defer 所有非 pk 字段，SELECT 只包含 pk。F2P 测试期望同时选择 `id` 和 `unique_field`，但 mutation 后只选 `id`，测试失败。测试 2（信号存在时选所有字段）不受影响。

---

### Group B — 替换

**原 mutation**：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index d28e596b46..1050b8e9f2 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -234,7 +234,7 @@ class Collector:
                         # as interactions between both features are hard to
                         # get right. This should only happen in the rare
                         # cases where .related_objects is overridden anyway.
-                        if not (sub_objs.query.select_related or self._has_signal_listeners(related_model)):
+                        if sub_objs.query.select_related or self._has_signal_listeners(related_model):
```

**分类**：🔴 必须替换

**理由**：条件倒置，原为"当既无 select_related 又无 signal 时才优化"，改后为"当有 select_related 或有 signal 时才优化"——这是对 golden patch 核心逻辑的直接逆操作。行为上完全反转了优化条件，在代码审查中会被立即识别。

**最终 mutation**（新设计）：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index d28e596b46..547f3138cd 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -118,10 +118,7 @@ class Collector:
             (field, value), set()).update(objs)

     def _has_signal_listeners(self, model):
-        return (
-            signals.pre_delete.has_listeners(model) or
-            signals.post_delete.has_listeners(model)
-        )
+        return signals.pre_delete.has_listeners(model)

     def can_fast_delete(self, objs, from_field=None):
         """
```

**变异语义**：`_has_signal_listeners` 仅检查 `pre_delete`，忽略 `post_delete`。当只有 `post_delete` 监听器时，方法返回 False，导致字段延迟优化被错误地应用——只选择 `referenced_fields` 而非全部字段。F2P 测试中 `post_delete` 子测试会失败（期望 `large_field` 在 SQL 中，但优化使其被 defer）。`pre_delete` 子测试通过，使 bug 更难发现。此外 `can_fast_delete` 也依赖 `_has_signal_listeners`，导致有 `post_delete` 监听器的模型也会走 fast-delete 路径（如果其他条件满足），进一步破坏正确性。这个 bug 模拟真实开发者写辅助函数时遗漏一种信号类型的错误。

---

### Group C — 替换

**原 mutation**：
```diff
-                            sub_objs = sub_objs.only(*tuple(referenced_fields))
+                            sub_objs = sub_objs
```

**分类**：🔴 必须替换

**理由**：虽然写法不同，但功能上等价于"不执行任何字段限制"，与直接删除这段优化逻辑效果相同。`sub_objs = sub_objs` 是无意义赋值，代码审查中显而易见。

**最终 mutation**（新设计）：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index d28e596b46..620a409818 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -234,7 +234,7 @@ class Collector:
                         # as interactions between both features are hard to
                         # get right. This should only happen in the rare
                         # cases where .related_objects is overridden anyway.
-                        if not (sub_objs.query.select_related or self._has_signal_listeners(related_model)):
+                        if not (sub_objs.query.select_related and self._has_signal_listeners(related_model)):
```

**变异语义**：将 `or` 改为 `and`，使跳过优化的条件从"有 select_related 或有 signal"（宽松）变为"有 select_related 且有 signal"（严格）。结果：即使有信号监听器但没有 select_related 时，字段优化仍会被应用。F2P 测试中，连接 pre_delete 或 post_delete 后，`_has_signal_listeners` 返回 True 但 `select_related` 为空，条件 `not (False and True)` = True，字段延迟优化被错误应用，`large_field` 被 defer，测试断言失败。逻辑运算符的误用是真实开发者在处理多条件判断时常见的错误。

---

### Group D — 替换

**原 mutation**：与 Group C 完全相同的 diff。

**分类**：🔴 必须替换

**理由**：与 Group C 完全重复，对多样性无贡献。

**最终 mutation**（新设计）：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index d28e596b46..08eeb68ff9 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -120,7 +120,7 @@ class Collector:
     def _has_signal_listeners(self, model):
         return (
             signals.pre_delete.has_listeners(model) or
-            signals.post_delete.has_listeners(model)
+            signals.post_delete.has_listeners(type(model))
         )

     def can_fast_delete(self, objs, from_field=None):
```

**变异语义**：`post_delete.has_listeners(model)` 被改为 `post_delete.has_listeners(type(model))`。在 Django 中，`model` 参数已经是一个类（`ModelBase` 实例），`type(model)` 返回的是元类 `ModelBase` 本身。`post_delete.has_listeners(ModelBase)` 会查找是否有注册到元类上的监听器，而非到具体模型类——这在正常使用中永远为 False。因此当 `post_delete` 信号有监听器时，`_has_signal_listeners` 仍返回 False（除非 `pre_delete` 也有监听器）。与 Group B 不同：B 是直接截断 return，D 是将检查转移到错误的对象上，看起来更像无意的误操作（混淆了对象与类型）。F2P 测试的 `post_delete` 子测试失败，`pre_delete` 子测试通过。

---

### Group E — 替换

**原 mutation**：
```diff
+    def __init__(self, using, optimize_deletion_fields=False):
         self.using = using
+        self.optimize_deletion_fields = optimize_deletion_fields
...
-        if not (sub_objs.query.select_related or self._has_signal_listeners(related_model)):
+        if self.optimize_deletion_fields and not (sub_objs.query.select_related or self._has_signal_listeners(related_model)):
```

**分类**：🔴 必须替换

**理由**：`optimize_deletion_fields=False` 永远是 False（没有任何地方传 True），导致优化永不生效。设计看起来像"可选功能开关"，但由于默认值为 False 且无调用方设为 True，功能被完全禁用。不自然：引入了无人使用的参数。

**最终 mutation**（新设计）：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index d28e596b46..ac967e6187 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -254,7 +254,7 @@ class Collector:
         """
         return related.related_model._base_manager.using(self.using).filter(
             **{"%s__in" % related.field.name: objs}
-        )
+        ).select_related(related.field.name)

     def instances_with_model(self):
         for model, instances in self.data.items():
```

**变异语义**：`related_objects()` 在返回 queryset 时附加 `.select_related(related.field.name)`，使所有通过该方法获取的关联对象 queryset 都带有 `select_related` 标记。在 `collect()` 中，条件 `sub_objs.query.select_related` 因此总为真（非空 dict/truthy），使 `if not (sub_objs.query.select_related or ...)` 永远为 False。字段延迟优化（`.only(*referenced_fields)`）永不被应用。F2P 测试第一个断言失败：期望的 SQL 只含 `id` 和 `unique_field`，但实际含所有字段（因为优化未触发）。这个 bug 位于不同函数（`related_objects`），通过跨函数的副作用影响 `collect()` 中的条件判断，较难追溯。模拟了真实开发者为"性能优化"添加 `select_related` 却破坏了字段延迟优化的场景。

## 新设计 Mutation 说明

### Group B 设计依据
`_has_signal_listeners` 是 golden patch 新引入的辅助方法，同时被 `can_fast_delete` 和 `collect` 调用。仅检查 `pre_delete` 而遗漏 `post_delete` 是真实开发者在重构信号检查逻辑时容易犯的错误（两个信号在语义上相似，容易只写一个）。此 bug 影响的位置在新引入的方法内部，不在核心条件判断处，更难被发现。而且由于 `pre_delete` 测试用例仍通过，只有 `post_delete` 子测试失败，整体来看像是偶发的覆盖不全问题。

### Group C 设计依据
将优化守卫条件中的逻辑运算符从 `or` 改为 `and` 是一个极其自然的单字符改动。正确语义为"无 select_related 且无 signal 才优化"（两个阻止条件），开发者可能误写为"需要同时满足两个阻止条件才不优化"（即 `and`）。这类布尔逻辑失误在处理否定条件组合时尤为常见（德摩根定律应用错误）。

### Group D 设计依据
`model` 在 Django 信号 API 中已经是 class 对象（`ModelBase` 实例），`type(model)` 返回元类。混淆"类"和"类的类型"是 Python 元编程中的经典陷阱。这个 bug 语法正确，`has_listeners` 接受任意对象，不会报错，只是静默返回 False。与 Group B 形成互补：B 是遗漏整个 post_delete，D 是用错误的对象调用 post_delete。

### Group E 设计依据
在 `related_objects` 中添加 `select_related` 看起来像是"为减少 N+1 查询而做的性能优化"，实际上与 `collect()` 中的字段延迟逻辑产生了隐藏的相互作用。这是典型的跨函数 bug：修改在 `related_objects`，破坏在 `collect()` 的条件判断，两处代码在代码审查中分开看都"合理"，组合起来才出问题。`related.field.name` 是外键字段名（如 `origin`），在 `Referrer.objects.filter(origin__in=...).select_related('origin')` 上似乎合理（预加载 origin 对象），但导致 `sub_objs.query.select_related` 非空，破坏字段延迟逻辑。
