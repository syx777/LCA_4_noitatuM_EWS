# django__django-12325

## 问题背景

当一个 MTI（多表继承）子模型同时声明了一个显式的 `parent_link=True` 的 `OneToOneField` 以及另一个指向同一父模型但**没有** `parent_link=True` 的 `OneToOneField` 时，Django 会抛出 `ImproperlyConfigured: Add parent_link=True to ...`。

根本原因是两处 bug 的组合：
1. `django/db/models/base.py` 中的 `__new__` 方法在收集 `parent_links` 时，没有检查 `field.remote_field.parent_link`，导致任何指向父模型的 `OneToOneField`（无论是否有 `parent_link=True`）都会被当作 parent link 登记到 `parent_links` 字典中。
2. `django/db/models/options.py` 中的 `_prepare` 方法里有一段多余的 `ImproperlyConfigured` 抛出逻辑，在 pk 尚未确定时如果 "promoted" 的字段没有 `parent_link`，就会报错。

Golden patch 同时修复了这两处：在 `base.py` 的过滤条件中加入 `and field.remote_field.parent_link`，同时删除 `options.py` 中多余的错误抛出代码。

## Golden Patch 语义分析

Golden patch 的核心逻辑修复：

1. **`base.py` 修改**：`parent_links` 字典应只记录显式声明了 `parent_link=True` 的 `OneToOneField`。之前没有此过滤，导致非 parent-link 的 OneToOneField 也被错误注册为 parent link，使得之后查找 `parent_links[base_key]` 时会错误地返回非 parent-link 字段。

2. **`options.py` 修改**：删除了用于强制要求 parent link 标记的 `ImproperlyConfigured` 检查。原始代码中，当 `already_created` 字段不含 `parent_link=True` 时，会抛出错误。但 `already_created` 查找和 `parent_links[base_key]` 查找是联动的——一旦 `base.py` 中错误地注册了非 parent-link 的 OneToOneField，`_prepare` 就会尝试把它 promote 为 pk，然后触发此错误。去掉这个检查使得行为更宽松，允许 parent link 由字段的 `parent_link` 属性本身决定。

## 调用链分析

```
ModelBase.__new__  (base.py)
  → 构建 parent_links 字典（遍历 base._meta.local_fields 中的 OneToOneField）
  → 遍历 new_class.mro() 中的 parent 类
      → 查找 parent_links[base_key] → 找到则用已有字段，否则 auto-create ptr 字段
      → new_class._meta.parents[base] = field
  → new_class._prepare()
      → Options._prepare(model)  (options.py)
          → 若 self.pk 为 None 且存在 parents
              → next(iter(self.parents.values())) 取第一个 parent link 字段
              → 查找 already_created，取已有字段
              → field.primary_key = True; self.setup_pk(field)
              → [已删除] 检查 field.remote_field.parent_link，否则报错
```

关键数据流：`parent_links` 字典（在 `base.py` 中构建）→ `_meta.parents` 字典（在 `base.py` 中填充）→ `Options._prepare` 中使用 `self.parents` 决定哪个字段成为 pk。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | 与 D、E 完全相同的 diff，是简单逆操作 |
| B | 全新设计 | 替换（新设计） | 原无此组，需补充 |
| C | 全新设计 | 替换（新设计） | 原无此组，需补充 |
| D | 必须替换 | 替换 | 与 A、E 完全相同的 diff，是简单逆操作 |
| E | 必须替换 | 替换 | 与 A、D 完全相同的 diff，是简单逆操作 |

所有原始3个 mutation 均为相同 diff（直接逆操作 golden patch 的 base.py 部分），属于**必须替换**。Groups B、C 在原始 mutations.jsonl 中缺失，需要全部新设计。共替换全部5个。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
diff --git a/django/db/models/base.py b/django/db/models/base.py
index 24453e218a..8ea6c05ef9 100644
--- a/django/db/models/base.py
+++ b/django/db/models/base.py
@@ -202,7 +202,7 @@ class ModelBase(type):
                 continue
             # Locate OneToOneField instances.
             for field in base._meta.local_fields:
-                if isinstance(field, OneToOneField) and field.remote_field.parent_link:
+                if isinstance(field, OneToOneField):
                     related = resolve_relation(new_class, field.remote_field.model)
                     parent_links[make_model_tuple(related)] = field
```

**分类**：🔴 必须替换

**理由**：直接逆操作 golden patch，等同于还原到 base_commit 原始 bug 代码，没有任何独立设计价值。A/D/E 三组完全相同，严重冗余。

**最终 mutation**：
```diff
diff --git a/django/db/models/options.py b/django/db/models/options.py
index 08c80bb6c8..694e029f22 100644
--- a/django/db/models/options.py
+++ b/django/db/models/options.py
@@ -242,7 +242,7 @@ class Options:
             if self.parents:
                 # Promote the first parent link in lieu of adding yet another
                 # field.
-                field = next(iter(self.parents.values()))
+                field = next(reversed(list(self.parents.values())))
                 # Look for a local field with the same name as the
                 # first parent link. If a local field has already been
                 # created, use it instead of promoting the parent
```

**变异语义**：`_prepare` 中原本取 `parents` 字典的**第一个**值作为 pk 候选字段（通常是最近一级父类的 parent link）。改为取**最后一个**（`reversed`）后，当模型有多个父类时，将错误地把最远祖先的 parent link 当作 pk，导致 `test_render` 等涉及多级继承的迁移状态测试失败。这个修改看起来语义正确（也许开发者想取"第一个定义的"parent），但实际颠倒了期望的行为。

---

### Group B — 替换（新设计）

**原 mutation**：（缺失）

**分类**：全新设计

**理由**：原 mutations.jsonl 中无 Group B，需要从头设计。选择在 `related.py` 的 `_check_clashes` 方法中引入 bug。

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/related.py b/django/db/models/fields/related.py
index f6c5ae2585..edf1da9020 100644
--- a/django/db/models/fields/related.py
+++ b/django/db/models/fields/related.py
@@ -251,7 +251,7 @@ class RelatedField(FieldCacheMixin, Field):
         # Check clashes between accessors/reverse query names of `field` and
         # any other field accessor -- i. e. Model.foreign accessor clashes with
         # Model.m2m accessor.
-        potential_clashes = (r for r in rel_opts.related_objects if r.field is not self)
+        potential_clashes = (r for r in rel_opts.related_objects if r.field is not self and not r.field.remote_field.parent_link)
```

**变异语义**：在 `_check_clashes` 中检查反向访问器冲突时，跳过所有 `parent_link=True` 的字段。这意味着当 `parent_ptr` 和另一个 `OneToOneField` 共享同一父模型时，parent_link 字段不会被列为 clash 候选，导致 `test_clash_parent_link` 测试中预期的 E304/E305 错误不再被生成。这个修改看起来很合理（"parent link 是系统内部字段，不应参与 clash 检测"），但实际上会导致真实的命名冲突被忽视。

---

### Group C — 替换（新设计）

**原 mutation**：（缺失）

**分类**：全新设计

**理由**：原 mutations.jsonl 中无 Group C，需要从头设计。选择在 `options.py` 的 `_prepare` 方法中修改 `already_created` 过滤逻辑。

**最终 mutation**：
```diff
diff --git a/django/db/models/options.py b/django/db/models/options.py
index 08c80bb6c8..66bf32bbbc 100644
--- a/django/db/models/options.py
+++ b/django/db/models/options.py
@@ -246,7 +246,7 @@ class Options:
                 # Look for a local field with the same name as the
                 # first parent link. If a local field has already been
                 # created, use it instead of promoting the parent
-                already_created = [fld for fld in self.local_fields if fld.name == field.name]
+                already_created = [fld for fld in self.local_fields if fld.name == field.name and not fld.primary_key]
```

**变异语义**：`already_created` 原本查找与 `field.name` 同名的已有字段，以使用用户显式声明的版本替代自动生成的 ptr 字段。加入 `and not fld.primary_key` 后，如果对应字段**已经**是 primary key，则 `already_created` 为空，系统将错误地使用 `parents` 字典中的原始字段（可能是自动生成的 ptr），而不是用户在迁移中显式指定的字段。这导致 `test_render`（迁移状态测试）中字段属性不匹配，特别是 `parent_link=True` 和 `serialize=False` 等属性的序列化行为发生变化。

---

### Group D — 替换

**原 mutation**：（与 A 相同，略）

**分类**：🔴 必须替换

**理由**：与 A、E 完全相同的 diff，直接逆操作。

**最终 mutation**：
```diff
diff --git a/django/db/models/base.py b/django/db/models/base.py
index 24453e218a..585838c23d 100644
--- a/django/db/models/base.py
+++ b/django/db/models/base.py
@@ -193,7 +193,7 @@ class ModelBase(type):
 
         # Collect the parent links for multi-table inheritance.
         parent_links = {}
-        for base in reversed([new_class] + parents):
+        for base in ([new_class] + parents):
             # Conceptually equivalent to `if base is Model`.
             if not hasattr(base, '_meta'):
                 continue
```

**变异语义**：`parent_links` 的构建使用 `reversed` 是为了让优先级从 `new_class` 开始（new_class 的字段最终覆盖父类的字段）。去掉 `reversed` 后，父类的字段会覆盖子类的显式声明，导致当子类声明了显式 parent link 时，父类的非 parent-link OneToOneField 可能错误地覆盖。这会影响 `test_onetoone_with_explicit_parent_link_parent_model` 等测试中字段查找的正确性。

---

### Group E — 替换

**原 mutation**：（与 A 相同，略）

**分类**：🔴 必须替换

**理由**：与 A、D 完全相同的 diff，直接逆操作。

**最终 mutation**：
```diff
diff --git a/django/db/models/base.py b/django/db/models/base.py
index 24453e218a..81ceb025cf 100644
--- a/django/db/models/base.py
+++ b/django/db/models/base.py
@@ -198,7 +198,7 @@ class ModelBase(type):
             if not hasattr(base, '_meta'):
                 continue
             # Skip concrete parent classes.
-            if base != new_class and not base._meta.abstract:
+            if base != new_class and not base._meta.abstract and not base._meta.parents:
                 continue
             # Locate OneToOneField instances.
             for field in base._meta.local_fields:
```

**变异语义**：原条件 `if base != new_class and not base._meta.abstract: continue` 跳过所有具体（非 abstract）父类，避免重复处理父类的字段。加入 `and not base._meta.parents` 后，如果一个父类自身也有父类（即中间层继承），则不再被跳过——其 `local_fields` 中的 OneToOneField 会被错误地加入 `parent_links`。这会影响多级继承场景：比如 `Child → Parent → GrandParent` 的继承链中，`Parent` 对 `GrandParent` 的 parent link 字段会被错误地注册到 `Child` 的 `parent_links` 中，导致 parent link 解析混乱。

## 新设计 Mutation 说明

### Group A（options.py `_prepare` - 迭代顺序反转）
基于对 `_prepare` 方法的分析：`self.parents` 是一个有序字典（Python 3.7+ 保证插入顺序），其值是 parent link 字段。`next(iter(...))` 取第一个元素，在单继承中是唯一的父类字段；在多继承中取的是最近的父类。改为 `next(reversed(list(...)))` 取最后一个，模拟了开发者误用迭代方向的错误，只在有多个父类时（罕见场景）出错。

### Group B（related.py `_check_clashes` - 跳过 parent_link 的 clash 检测）
基于对 `_check_clashes` 的分析：该方法检查两个 related field 之间的访问器名称冲突。当子模型有 parent_ptr 和另一个 OneToOneField 时，二者的反向访问器可能冲突（这正是新 F2P 测试 `test_clash_parent_link` 要验证的）。通过排除 parent_link 字段的 clash 检测，使得这种冲突被静默忽略，测试预期的错误列表不再被生成。

### Group C（options.py `already_created` - primary_key 过滤）
基于对迁移状态 `test_render` 测试的分析：测试中创建的模型有显式的 parent link 字段，该字段在迁移中已被指定为 `primary_key=True`。加入 `not fld.primary_key` 过滤后，这个已经是 pk 的字段被排除在 `already_created` 之外，系统将回退到使用 parents 字典中的初始字段，该字段可能缺少 `parent_link=True` 标记，导致序列化时 `parent_link=True` 被省略，破坏迁移状态的 repr。

### Group D（base.py `parent_links` - 移除 reversed）
基于对 parent_links 字典构建逻辑的分析：`reversed` 保证 new_class 的字段最终写入字典（因为先写父类再写子类，子类会覆盖父类）。去掉后，父类的字段最后写入，覆盖子类的显式声明，导致当子类显式指定 parent link 时无法被正确识别。

### Group E（base.py skip条件 - 中间层继承不跳过）
基于对 MTI 多级继承的分析：`if base != new_class and not base._meta.abstract: continue` 确保只处理 new_class 自身的字段（以及 abstract base 的字段）。加入 `and not base._meta.parents` 后，中间层（有父类的非 abstract 类）不再被跳过，其字段会被错误地加入 parent_links，在多级继承场景中引起 parent link 错误关联。
