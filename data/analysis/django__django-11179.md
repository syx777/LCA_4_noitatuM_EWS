# django__django-11179

## 问题背景

当一个没有任何依赖关系的模型实例被删除时（即走"快速删除"路径），`delete()` 方法不会将该实例的主键（PK）设置为 `None`。这违背了 Django 的一贯语义：删除操作后，实例的 PK 应该被清空，以表示该对象不再持久化于数据库中。

golden patch 在 `Collector.delete()` 的快速删除分支（第 277-280 行）中，在 `return` 语句之前添加了：

```python
setattr(instance, model._meta.pk.attname, None)
```

这使快速删除路径与非快速删除路径（第 326 行）保持一致。

## Golden Patch 语义分析

`Collector.delete()` 有两条执行路径：

1. **快速删除路径**（第 275-280 行）：当 `self.data` 中只有一个模型且只有一个实例，且该实例可以快速删除时，直接调用 `sql.DeleteQuery.delete_batch()` 然后 return。修复前，此路径没有清空 PK。

2. **普通删除路径**（第 282-327 行）：通过 `transaction.atomic` 处理多对象、多模型的复杂删除，最后在第 325-326 行遍历所有实例并清空 PK。

Bug 根因：快速删除路径是一个性能优化捷径，开发者在实现时遗漏了"删除后清空 PK"这一语义操作，导致两路径行为不一致。

`model._meta.pk.attname` 与 `model._meta.pk.name` 的区别：对于普通 AutoField（如 `id`），两者相同；但对于通过外键实现的父指针（如多表继承），`attname` 是实际的字段存储名（如 `parent_ptr_id`），而 `name` 是关系名（如 `parent_ptr`）。因此必须使用 `attname`。

## 调用链分析

```
Model.delete()
  └── Collector.collect(objs)
        ├── Collector.can_fast_delete(objs) → True → self.fast_deletes.append(objs)
        └── (否则) Collector.add(objs) → self.data[model].add(objs)
  └── Collector.delete()
        ├── [快速路径] can_fast_delete(instance) → delete_batch → setattr(pk=None) → return
        └── [普通路径] transaction.atomic → delete_batch per model → post_delete signals → setattr(pk=None)
```

`can_fast_delete` 检查：无级联、无父模型、无信号监听器、无 generic FK。
`delete_batch` 执行实际的 SQL DELETE。
`setattr(instance, model._meta.pk.attname, None)` 在内存中清空 PK，反映数据库状态。

## 替换决策总览

原始 mutations.jsonl 中只有 3 条（A/C/D），均为低质量，全部替换；B/E 缺失，需新设计。

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | `pass  # PK clearing disabled` 含明显注释人工痕迹 |
| B | 缺失 | 新设计 | mutations.jsonl 中无 B 组条目 |
| C | 🔴 必须替换 | 替换 | 注释掉代码行，明显人工痕迹 |
| D | 🔴 必须替换 | 替换 | 直接删除 setattr 行，等同于 golden patch 的逆操作 |
| E | 缺失 | 新设计 | mutations.jsonl 中无 E 组条目 |

语义浅层共 0 个（所有现存 mutation 均属于必须替换或缺失）。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
-                setattr(instance, model._meta.pk.attname, None)
+                pass  # PK clearing disabled
```
**分类**：🔴 必须替换  
**理由**：注释 "PK clearing disabled" 是明显的人工痕迹，真实代码中不会出现这种注释。在代码审查中立即可识别为故意引入的 bug。  
**最终 mutation**：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index 3a462f1c4c..61c96ff840 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -277,7 +277,7 @@ class Collector:
             if self.can_fast_delete(instance):
                 with transaction.mark_for_rollback_on_error():
                     count = sql.DeleteQuery(model).delete_batch([instance.pk], self.using)
-                setattr(instance, model._meta.pk.attname, None)
+                setattr(instance, model._meta.pk.attname, 0)
                 return count, {model._meta.label: count}
 
         with transaction.atomic(using=self.using, savepoint=False):
```
**变异语义**：将 PK 清零为 `0` 而非 `None`。`0` 在 Python 中是 falsy，很多代码会用 `if instance.pk:` 判断，此时会误认为实例已删除；但 `assertIsNone(instance.pk)` 这类严格断言会失败。模拟了开发者用 `0` 作为"无效值"的认知错误。大多数涉及 `if obj.pk` 的测试会通过，只有精确测试 `pk is None` 的测试才会失败。

---

### Group B — 新设计（条件反转）
**原 mutation**：（无，缺失）  
**分类**：新设计  
**理由**：需要为 B 组（逻辑条件变异）设计一个高质量 mutation。  
**最终 mutation**：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index 3a462f1c4c..68f100054a 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -277,7 +277,8 @@ class Collector:
             if self.can_fast_delete(instance):
                 with transaction.mark_for_rollback_on_error():
                     count = sql.DeleteQuery(model).delete_batch([instance.pk], self.using)
-                setattr(instance, model._meta.pk.attname, None)
+                if not count:
+                    setattr(instance, model._meta.pk.attname, None)
                 return count, {model._meta.label: count}
 
         with transaction.atomic(using=self.using, savepoint=False):
```
**变异语义**：只有当删除数量为 0（即没有实际删除任何行）时才清空 PK。逻辑上，开发者可能认为"如果什么都没删，才需要清空 PK 以防止重复删除"——实际上逻辑完全相反。`count` 在成功删除后为 1，`not count` 为 False，导致 setattr 永远不执行。在功能测试（删除成功的场景）下全部失败，但代码看起来合理（像是防御性编程）。

---

### Group C — 替换
**原 mutation**：
```diff
-                setattr(instance, model._meta.pk.attname, None)
+#                 setattr(instance, model._meta.pk.attname, None)
```
**分类**：🔴 必须替换  
**理由**：注释掉代码行是最明显的人工痕迹，任何代码审查者一眼即见。  
**最终 mutation**：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index 3a462f1c4c..c4addfcf0f 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -277,7 +277,7 @@ class Collector:
             if self.can_fast_delete(instance):
                 with transaction.mark_for_rollback_on_error():
                     count = sql.DeleteQuery(model).delete_batch([instance.pk], self.using)
-                setattr(instance, model._meta.pk.attname, None)
+                setattr(model._meta.pk, 'default', None)
                 return count, {model._meta.label: count}
 
         with transaction.atomic(using=self.using, savepoint=False):
```
**变异语义**：将 setattr 的目标对象从 `instance`（模型实例）改为 `model._meta.pk`（字段对象本身），且属性名从 `attname`（如 `"id"`）改为 `'default'`。这实际上将字段的 `default` 属性清空，对实例的 PK 值毫无影响。模拟了开发者混淆了"清空字段的默认值"与"清空实例的字段值"两种操作。代码语法完全合法，逻辑上也看似有意义（"清空 pk 字段的默认值"），但实际上是操作错了对象。

---

### Group D — 替换
**原 mutation**：
```diff
-                setattr(instance, model._meta.pk.attname, None)
 # （直接删除该行）
```
**分类**：🔴 必须替换（等同于 golden patch 的直接逆操作）  
**理由**：直接删除 golden patch 新增的行，是最浅层的逆操作，不具备真实开发者犯错的语义真实性。  
**最终 mutation**：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index 3a462f1c4c..c806d3e15c 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -277,7 +277,7 @@ class Collector:
             if self.can_fast_delete(instance):
                 with transaction.mark_for_rollback_on_error():
                     count = sql.DeleteQuery(model).delete_batch([instance.pk], self.using)
-                setattr(instance, model._meta.pk.attname, None)
+                setattr(instance, model._meta.pk.attname + '_id', None)
                 return count, {model._meta.label: count}
 
         with transaction.atomic(using=self.using, savepoint=False):
```
**变异语义**：将属性名从 `model._meta.pk.attname`（如 `"id"`）改为 `model._meta.pk.attname + '_id'`（如 `"id_id"`）。这是对 Django 外键命名约定的误用：Django ForeignKey 字段的 `attname` 通常是 `fieldname_id`，开发者可能误以为 PK 的存储属性也需要 `_id` 后缀。对于普通 AutoField，这会在实例上新建一个动态属性 `id_id=None`，而真正的 `id` 字段不受影响，PK 保持原值。代码读起来像是"正确地"处理了外键场景，但实际上引入了属性名错误。

---

### Group E — 新设计（错误的条件守卫）
**原 mutation**：（无，缺失）  
**分类**：新设计  
**理由**：需要为 E 组（状态/属性条件变异）设计一个高质量 mutation。  
**最终 mutation**：
```diff
diff --git a/django/db/models/deletion.py b/django/db/models/deletion.py
index 3a462f1c4c..5959d141ab 100644
--- a/django/db/models/deletion.py
+++ b/django/db/models/deletion.py
@@ -277,7 +277,8 @@ class Collector:
             if self.can_fast_delete(instance):
                 with transaction.mark_for_rollback_on_error():
                     count = sql.DeleteQuery(model).delete_batch([instance.pk], self.using)
-                setattr(instance, model._meta.pk.attname, None)
+                if model._meta.auto_created:
+                    setattr(instance, model._meta.pk.attname, None)
                 return count, {model._meta.label: count}
 
         with transaction.atomic(using=self.using, savepoint=False):
```
**变异语义**：在 setattr 前添加 `if model._meta.auto_created:` 守卫。`auto_created` 在 Django 中标记了自动生成的中间表（如 M2M 关系的透明连接表），对于用户定义的普通模型（如 `User`），该属性为 `False`。因此条件永远不满足，PK 永远不被清空。这模拟了开发者将信号发送逻辑（非快速删除路径中有 `if not model._meta.auto_created:` 守卫用于信号）错误地移植到了 PK 清空逻辑中，且方向还弄反了。非常隐蔽：`auto_created` 是真实的 Django 元属性，条件结构也符合其他代码的模式，只有深刻了解 `auto_created` 语义才能发现错误。

## 新设计 Mutation 说明

**Group B**（`if not count:`）：基于对 `delete_batch` 返回值语义的分析。`delete_batch` 返回实际删除的行数，正常情况为 1。开发者可能参考了"幂等删除"模式，认为"如果本次删除了数据（count > 0），PK 应保留供日志/回调使用；只有在没删成功时才清空以防止重试"——这是语义上合理但方向完全错误的逻辑。

**Group E**（`if model._meta.auto_created:`）：基于对 `delete()` 方法中其他 `auto_created` 用法的分析。第 285 行有 `if not model._meta.auto_created:` 用于控制信号发送。开发者可能在添加 PK 清空逻辑时参考了信号逻辑，但（1）忘记取反，（2）误以为 PK 清空也应该遵循相同的模型过滤规则。这是真实的跨代码模式混用错误。
