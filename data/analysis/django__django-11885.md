# django__django-11885

## 问题背景

Issue 要求优化 Django ORM 的级联删除性能：当多个外键字段（ForeignKey）指向同一个 related model 时，`Collector` 会对每个 FK 字段分别发出一条 `DELETE FROM table WHERE field_id = :id` 查询，造成 N 次数据库往返。修复的核心思路是将这些查询合并为一条带 OR 条件的 DELETE 语句：`DELETE FROM table WHERE field1_id = :id OR field2_id = :id`。

测试模型示例：
```python
class SecondReferrer(models.Model):
    referrer = models.ForeignKey(Referrer, models.CASCADE)
    other_referrer = models.ForeignKey(Referrer, models.CASCADE, to_field='unique_field', related_name='+')
```
删除一个 `Referrer` 实例时，`SecondReferrer` 有两个 FK 指向它，应合并为 1 条 DELETE 而不是 2 条。

## Golden Patch 语义分析

Golden patch 做了以下核心改动：

1. **`related_objects(related_model, related_fields, objs)`** — 签名改为接受多个 fields（列表），内部用 `reduce(operator.or_, Q(...))` 生成 OR 谓词，一次查询覆盖所有 FK 关系。

2. **`collect()` 引入 `model_fast_deletes = defaultdict(list)`** — 先把所有可快速删除的 related model 按 `related_model`（类）分组，收集各 FK 字段；遍历完后，再对每个 related_model 一次性生成合并查询。

3. **`get_del_batches(objs, fields)`** — 参数从单个 `field` 改为 `fields` 列表，支持多字段的批量大小计算。

4. **`django/contrib/admin/utils.py` 中的 `related_objects` 覆盖** — 跟随 base class 的签名变化做相应调整，select_related 扩展为所有 related_fields。

5. **其他**：`self.data`, `self.field_updates`, `self.dependencies` 从普通 `dict` 改为 `defaultdict`，简化各 `setdefault` 调用。

**关键语义**：`model_fast_deletes` 按 `related_model` 类作为键聚合字段，保证同一 related model 的多个 FK 字段最终生成 1 个合并 queryset，而不是每字段 1 个。

## 调用链分析

```
Referrer.delete()
  └── Collector.collect(referrer_instance)
        ├── can_fast_delete(referrer_instance)  → False (Referrer itself has related objects)
        ├── add([referrer])  → self.data[Referrer] = {referrer}
        └── collect_related=True 分支:
              ├── get_candidate_relations_to_delete(Referrer._meta)
              │     → [SecondReferrer.referrer (FK), SecondReferrer.other_referrer (FK)]
              ├── can_fast_delete(SecondReferrer, from_field=referrer_field)  → True
              │     → model_fast_deletes[SecondReferrer].append(referrer_field)
              ├── can_fast_delete(SecondReferrer, from_field=other_referrer_field)  → True
              │     → model_fast_deletes[SecondReferrer].append(other_referrer_field)
              └── 处理 model_fast_deletes:
                    → related_objects(SecondReferrer, [referrer_field, other_referrer_field], batch)
                    → Q(referrer__in=batch) | Q(other_referrer__in=batch)
                    → fast_deletes.append(combined_qs)   ← 1 个 queryset

Collector.delete()
  ├── fast_deletes: [combined_SecondReferrer_qs]  → 1 条 DELETE
  └── data: {Referrer: [referrer]}               → 1 条 DELETE
  共 2 条查询
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失 | 新增 | mutations.jsonl 中无 Group A 条目 |
| B | 🟡 语义浅层 | 保留 | `not` 取反位于关键判断节点，能产生真实错误行为（破坏 fast-delete 分组逻辑） |
| C | 缺失 | 新增 | mutations.jsonl 中无 Group C 条目 |
| D | 🔴 必须替换 | 替换 | 注释掉 `self.fast_deletes = []`，初始化即 AttributeError，不自然且必然崩溃 |
| E | 🔴 必须替换 | 替换 | 添加 `combine_fast_deletes=False` flag，本质上是 undo patch 的逆操作，功能等价于还原旧代码 |

语义浅层共 1 个（Group B），floor(1/2) = 0 个替换，全部保留。

## 各组 Mutation 分析

### Group A — 新增

**原 mutation**：（缺失，mutations.jsonl 无此组）

**分类**：新增

**理由**：需要补全 Group A（API Specifications & Contracts）策略组。

**最终 mutation**：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index 62afff1f50..c493ce19bb 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -227,7 +227,7 @@ class Collector:
                 continue
             related_model = related.related_model
             if self.can_fast_delete(related_model, from_field=field):
-                model_fast_deletes[related_model].append(field)
+                model_fast_deletes[related_model, field.name].append(field)
                 continue
             batches = self.get_del_batches(new_objs, [field])
             for batch in batches:
@@ -246,7 +246,7 @@ class Collector:
                     sub_objs = sub_objs.only(*tuple(referenced_fields))
                 if sub_objs:
                     field.remote_field.on_delete(self, field, sub_objs, self.using)
-        for related_model, related_fields in model_fast_deletes.items():
+        for (related_model, _), related_fields in model_fast_deletes.items():
             batches = self.get_del_batches(new_objs, related_fields)
             for batch in batches:
                 sub_objs = self.related_objects(related_model, related_fields, batch)
```

**变异语义**：将 `model_fast_deletes` 的键从 `related_model`（类对象）改为 `(related_model, field.name)` 复合键。API 契约的核心是"按 related_model 分组合并所有 FK 字段"，而此 mutation 破坏了这一契约：每个 FK 字段单独成组，`model_fast_deletes` 退化为"每 FK 一个 list"的结构，最终每个 FK 生成 1 个独立 queryset，产生 3 条查询（2 SecondReferrer + 1 Referrer）而非 2 条。代码看起来只是"key 的粒度更细了"，审查者不易察觉其对合并语义的破坏。

---

### Group B — 保留

**原 mutation**：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index 62afff1f50..0c65177909 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -226,7 +226,7 @@ class Collector:
             if field.remote_field.on_delete == DO_NOTHING:
                 continue
             related_model = related.related_model
-            if self.can_fast_delete(related_model, from_field=field):
+            if not self.can_fast_delete(related_model, from_field=field):
                 model_fast_deletes[related_model].append(field)
                 continue
             batches = self.get_del_batches(new_objs, [field])
```

**分类**：🟡 语义浅层（保留）

**理由**：虽然是单个 `not` 取反（语义浅层），但修改位置是 fast-delete 分支判断的核心节点。取反后，原本 fast-deletable 的字段进入慢路径（触发 `if sub_objs:` 查询），原本不 fast-deletable 的字段反而被加入 `model_fast_deletes` 后走 fast-delete 路径，会产生 3 条以上查询使 F2P 测试失败。此变异能模拟真实的逻辑判断失误，保留价值高。

**最终 mutation**：与原相同（保留）。

**变异语义**：fast-deletable 的 related model 不再走快路径，而是通过 `if sub_objs:` 触发 SELECT 查询；非 fast-deletable 的错误地走了 fast-delete 路径，可能跳过必要的 CASCADE 回调。产生额外查询数，`assertNumQueries(2)` 将失败。

---

### Group C — 新增

**原 mutation**：（缺失，mutations.jsonl 无此组）

**分类**：新增

**理由**：需要补全 Group C（Type & Data Shape）策略组。

**最终 mutation**：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index 62afff1f50..81d7210ab5 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -217,7 +217,7 @@ class Collector:
 
         if keep_parents:
             parents = set(model._meta.get_parent_list())
-        model_fast_deletes = defaultdict(list)
+        model_fast_deletes = []
         for related in get_candidate_relations_to_delete(model._meta):
             # Preserve parent reverse relationships if keep_parents=True.
             if keep_parents and related.model in parents:
@@ -227,7 +227,7 @@ class Collector:
                 continue
             related_model = related.related_model
             if self.can_fast_delete(related_model, from_field=field):
-                model_fast_deletes[related_model].append(field)
+                model_fast_deletes.append((related_model, field))
                 continue
             batches = self.get_del_batches(new_objs, [field])
             for batch in batches:
@@ -246,10 +246,10 @@ class Collector:
                     sub_objs = sub_objs.only(*tuple(referenced_fields))
                 if sub_objs:
                     field.remote_field.on_delete(self, field, sub_objs, self.using)
-        for related_model, related_fields in model_fast_deletes.items():
-            batches = self.get_del_batches(new_objs, related_fields)
+        for related_model, field in model_fast_deletes:
+            batches = self.get_del_batches(new_objs, [field])
             for batch in batches:
-                sub_objs = self.related_objects(related_model, related_fields, batch)
+                sub_objs = self.related_objects(related_model, [field], batch)
                 self.fast_deletes.append(sub_objs)
         for field in model._meta.private_fields:
             if hasattr(field, 'bulk_related_objects'):
```

**变异语义**：将 `model_fast_deletes` 的数据类型从 `defaultdict(list)`（字典，按 related_model 分组）改为普通 `list`（存储 `(related_model, field)` 元组）。类型变化破坏了 related_model 维度的分组语义：每个 `(model, field)` 元组单独处理，`related_objects` 每次只接收 `[field]`（单字段列表），OR 合并逻辑不再被激活，每个 FK 字段各自产生 1 个 fast-delete queryset。对于 SecondReferrer 的 2 个 FK，产生 3 条查询而非 2 条。这是一种数据结构选择导致的语义丢失，代码外观上"平铺列表也能存储同样信息"，不易被快速审查发现。

---

### Group D — 替换

**原 mutation**：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index 62afff1f50..73641a819f 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -72,7 +72,7 @@ class Collector:
         self.field_updates = defaultdict(partial(defaultdict, set))
         # fast_deletes is a list of queryset-likes that can be deleted without
         # fetching the objects into memory.
-        self.fast_deletes = []
+        # self.fast_deletes = []
```

**分类**：🔴 必须替换

**理由**：注释掉 `self.fast_deletes = []`，导致 `Collector` 实例没有 `fast_deletes` 属性，任何调用 `self.fast_deletes.append(...)` 的地方都会抛出 `AttributeError`。这是不自然的（代码审查中注释掉初始化代码非常可疑）且必然立即崩溃，无法通过任何测试。

**最终 mutation**：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index 62afff1f50..648fff5d06 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -228,7 +228,6 @@ class Collector:
             related_model = related.related_model
             if self.can_fast_delete(related_model, from_field=field):
                 model_fast_deletes[related_model].append(field)
-                continue
             batches = self.get_del_batches(new_objs, [field])
             for batch in batches:
                 sub_objs = self.related_objects(related_model, [field], batch)
```

**变异语义**：删除 `model_fast_deletes[related_model].append(field)` 之后的 `continue`，使 fast-deletable 字段在加入 `model_fast_deletes` 后不退出循环，继续执行慢路径代码。对于 SecondReferrer 的 2 个 FK，慢路径中 `if sub_objs:` 会触发 2 次 SELECT（检查是否有匹配对象，0 条记录时返回 False 但查询已执行）。加上 1 次合并 DELETE（来自 model_fast_deletes）和 1 次 Referrer DELETE，共 4 次查询。`assertNumQueries(2)` 将失败。这模拟了开发者遗漏 `continue` 语句的真实错误，代码逻辑看起来只是"条件分支后代码继续执行"而非明显的错误。

---

### Group E — 替换

**原 mutation**：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index 62afff1f50..908c0c754d 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -64,8 +64,9 @@ def get_candidate_relations_to_delete(opts):
 class Collector:
-    def __init__(self, using):
+    def __init__(self, using, combine_fast_deletes=False):
         self.using = using
+        self.combine_fast_deletes = combine_fast_deletes
...
+                if self.combine_fast_deletes:
+                    sub_objs = ...
+                    self.fast_deletes.append(sub_objs)
+                else:
+                    for field in related_fields:
+                        sub_objs = ...
+                        self.fast_deletes.append(sub_objs)
```

**分类**：🔴 必须替换

**理由**：添加 `combine_fast_deletes=False` 参数，默认 False 时走旧的逐字段路径，本质上是用 flag 包裹的 patch 逆操作，行为等价于还原合并优化。任何未显式传 `combine_fast_deletes=True` 的调用方都得不到合并效果，测试失败原因与"直接还原 patch"一样。

**最终 mutation**：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index 62afff1f50..7afd2c6706 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -261,11 +261,14 @@ class Collector:
         """
         Get a QuerySet of the related model to objs via related fields.
         """
-        predicate = reduce(operator.or_, (
-            query_utils.Q(**{'%s__in' % related_field.name: objs})
-            for related_field in related_fields
-        ))
-        return related_model._base_manager.using(self.using).filter(predicate)
+        base_qs = related_model._base_manager.using(self.using)
+        if len(related_fields) == 1:
+            return base_qs.filter(**{'%s__in' % related_fields[0].name: objs})
+        querysets = [
+            base_qs.filter(**{'%s__in' % rf.name: objs})
+            for rf in related_fields
+        ]
+        return querysets[0].union(*querysets[1:])
 
     def instances_with_model(self):
         for model, instances in self.data.items():
```

**变异语义**：将 `related_objects()` 中的 OR 谓词（`reduce(operator.or_, Q(...))` + 单次 `.filter()`）替换为 `.union()` 合并多个 queryset。在单字段时逻辑正确（快速路径），多字段时返回 union queryset。Django 的 union queryset 不支持 `._raw_delete()` 操作，调用 `fast_deletes` 中的 `qs._raw_delete(using=self.using)` 时会抛出异常（`TypeError: Calling QuerySet.delete() after .union() is not supported`）。这模拟了开发者"用 union 合并结果"而不知道 union queryset 无法删除的真实错误。

---

## 新设计 Mutation 说明

### Group A 设计依据

`model_fast_deletes` 字典的设计意图是"按 related_model 聚合所有指向它的 FK 字段"。键为 `related_model` 类对象，保证同一 related model 的多条 FK 字段聚集为一个 `list`。将键改为 `(related_model, field.name)` 复合 tuple 后，`defaultdict(list)` 仍然合法工作（每个复合键对应一个 list），但由于 `field.name` 不同，两个 FK 不再聚合到同一个 list 中。

后续处理循环 `for (related_model, _), related_fields in model_fast_deletes.items():` 能够正确解包 tuple 键，代码语法上完全合法，但 `related_fields` 每次只有 1 个元素，`related_objects` 的 OR 合并逻辑永远不会被激活。表面上看只是"key 更精细了"，实际上完全摧毁了合并优化。

### Group C 设计依据

将数据类型从 `defaultdict(list)`（group-by 字典）改为 flat `list`（线性存储）是一种常见的"简化数据结构"重构冲动。Flat list 存储 `(related_model, field)` 元组同样能记录相同的信息，遍历处理也同样合法，但失去了按 related_model 自动聚合的能力。后续循环变为 `for related_model, field in model_fast_deletes:` + 单字段处理，等价于完全放弃合并。

### Group D 设计依据

`continue` 语句在条件分支里是关键流控：fast-deletable 字段加入 `model_fast_deletes` 后必须 `continue`，否则同一字段也会走慢路径。遗漏 `continue` 是很自然的开发者疏忽——在读代码时，`if self.can_fast_delete(...):` 分支在逻辑上"处理了"该字段，但由于没有 `continue`，它仍然进入下面的 `batches = self.get_del_batches(...)` 代码段。慢路径中的 `if sub_objs:` 会执行真实的数据库查询来检验结果集是否为空，即使为空也消耗一次查询。对于 SecondReferrer 的 2 个 FK，额外产生 2 次 SELECT + 原本的 1 次合并 DELETE + 1 次 Referrer DELETE = 4 次查询。

### Group E 设计依据

`.union()` 是 Django ORM 中合并多个 queryset 的标准工具，开发者很自然地会想到用它来代替手写 `reduce(operator.or_, Q(...))` 的 OR 过滤。单字段情况下逻辑正确（快路径返回普通 queryset），多字段时返回 union queryset，语义上"看起来正确"（union 确实能获取所有匹配对象的并集）。但 union queryset 在 Django 中不支持删除操作，`qs._raw_delete()` 会抛出异常。这个 bug 只在多字段合并场景下触发，单字段 FK 的简单删除完全不受影响，难以通过简单测试发现。
