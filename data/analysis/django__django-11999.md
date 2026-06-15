# django__django-11999

## 问题背景

Django 2.2 引入了一个回归：用户在 Model 子类中定义的 `get_FOO_display()` 方法会被 Django 自动生成的版本覆盖，导致自定义方法无法生效。该问题在 Django 2.1 中正常工作。

Golden patch 的修复核心：在 `Field.contribute_to_class()` 中，将无条件的 `setattr` 改为先用 `hasattr` 检查目标类上是否已存在该方法，若已存在则不覆盖，从而允许用户重写。

```diff
-            setattr(cls, 'get_%s_display' % self.name,
-                    partialmethod(cls._get_FIELD_display, field=self))
+            if not hasattr(cls, 'get_%s_display' % self.name):
+                setattr(
+                    cls,
+                    'get_%s_display' % self.name,
+                    partialmethod(cls._get_FIELD_display, field=self),
+                )
```

## Golden Patch 语义分析

**为什么原代码有问题**：`contribute_to_class()` 在 Model 元类处理期间被调用，此时 Model 子类的所有方法已全部定义在类的 `__dict__` 中。原代码无条件地用 `setattr` 覆盖 `get_foo_bar_display`，这会将用户在类体中定义的同名方法替换为 Django 内部的 `partialmethod(cls._get_FIELD_display, field=self)`。

**为什么 `hasattr` 检查是正确的**：`hasattr(cls, 'get_%s_display' % self.name)` 在 `contribute_to_class` 执行时，能够看到用户在类体中定义的方法（因为类已构造完成）。若用户定义了该方法，`hasattr` 返回 True，`not hasattr` 为 False，跳过 `setattr`，保留用户方法。若用户未定义，`hasattr` 返回 False，正常设置 Django 默认方法。

**关键约束**：该检查必须针对 `cls` 本身（而非父类、元类或 MRO 的任意部分）。任何将检查目标偏移到其他对象的变体都会破坏此保护机制。

## 调用链分析

```
Model.__new__ (metaclass ModelBase)
  └── field.contribute_to_class(cls, name)          # Field.__init__.py:748
        ├── self.set_attributes_from_name(name)      # 设置 self.name, self.attname
        ├── cls._meta.add_field(self)
        ├── setattr(cls, self.attname, descriptor)   # 字段描述符
        └── [if self.choices is not None]
              └── [if not hasattr(cls, 'get_FOO_display')]
                    └── setattr(cls, 'get_FOO_display',
                                partialmethod(cls._get_FIELD_display, field=self))
                                          # ↓ 调用 base.py
                        └── Model._get_FIELD_display(self, field)  # base.py:941
                              ├── value = getattr(self, field.attname)
                              └── return force_str(dict(field.flatchoices).get(value, value))
                                                    # ↓ 调用 fields/__init__.py
                                        └── Field.flatchoices (property)  # __init__.py:863
                                              └── Field._get_flatchoices()
```

**数据流**：`IntegerField(choices=[(1,'foo'),(2,'bar')])` → `contribute_to_class` → 在 `cls` 上注册 `get_foo_bar_display` → 调用时 `_get_FIELD_display` 用 `field.flatchoices` 查找显示值。

**关键点**：`partialmethod(cls._get_FIELD_display, field=self)` 将 `field` 绑定为关键字参数。`self.name` 已由 `set_attributes_from_name` 设置，`contribute_to_class` 完成时 `self.model = cls`。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 直接条件取反，`hasattr→not hasattr` 导致只在方法存在时才覆盖，且未定义时完全不设置 |
| B | 🔴 必须替换 | 替换 | 与 A 完全相同的 diff，重复且同样是直接功能逆转 |
| C | 🔴 必须替换 | 替换 | `if True: # BUG: ...` 注释含"BUG"字样，极度不自然，明显人工痕迹 |
| D | 🔴 必须替换 | 替换 | 检查 `_get_foo_bar_display`（永不存在）等价于总是True，功能等价于删除保护 |
| E | 🔴 必须替换 | 替换 | `if True:` 死代码，任何代码审查者都会立刻发现这是异常条件 |

语义浅层共 0 个，全部5个均为必须替换。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index fe550169bd..212efe7485 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -763,7 +763,7 @@ class Field(RegisterLookupMixin):
             if not getattr(cls, self.attname, None):
                 setattr(cls, self.attname, self.descriptor_class(self))
         if self.choices is not None:
-            if not hasattr(cls, 'get_%s_display' % self.name):
+            if hasattr(cls, 'get_%s_display' % self.name):
```
**分类**：🔴 必须替换

**理由**：直接将 `not hasattr` 改为 `hasattr`，使逻辑完全反转：只有当方法已存在时才调用 `setattr`（覆盖用户方法），方法不存在时反而不设置（破坏正常功能）。这是对 golden patch 的直接功能逆转，且会导致所有没有自定义方法的普通模型的 `get_FOO_display` 也不会被设置，破坏面更广，容易被测试发现。

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index fe550169bd..87f8e67347 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -763,7 +763,7 @@ class Field(RegisterLookupMixin):
             if not getattr(cls, self.attname, None):
                 setattr(cls, self.attname, self.descriptor_class(self))
         if self.choices is not None:
-            if not hasattr(cls, 'get_%s_display' % self.name):
+            if not hasattr(cls.__bases__[0], 'get_%s_display' % self.name):
                 setattr(
                     cls,
                     'get_%s_display' % self.name,
```
**变异语义**：将 `hasattr` 检查目标从 `cls`（当前类）偷换为 `cls.__bases__[0]`（直接父类）。对于 `class FooBar(models.Model)`，`__bases__[0]` 是 `models.Model`，而 `Model` 从不拥有字段特定的 `get_foo_bar_display` 方法，因此该条件永远为 True，导致 Django 始终覆盖用户定义的方法。代码看起来像是开发者试图"检查继承链中是否已有实现"，混淆了检查目标。通过所有不涉及方法重写的 P2P 测试，仅在 `test_overriding_FIELD_display` 及类似继承重写测试下失败。

---

### Group B — 替换
**原 mutation**：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index fe550169bd..212efe7485 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -763,7 +763,7 @@ class Field(RegisterLookupMixin):
             if not getattr(cls, self.attname, None):
                 setattr(cls, self.attname, self.descriptor_class(self))
         if self.choices is not None:
-            if not hasattr(cls, 'get_%s_display' % self.name):
+            if hasattr(cls, 'get_%s_display' % self.name):
```
**分类**：🔴 必须替换

**理由**：与 Group A 完全相同的 diff，除重复问题外，同样属于直接功能逆转。

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index fe550169bd..ab65921f68 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -763,7 +763,7 @@ class Field(RegisterLookupMixin):
             if not getattr(cls, self.attname, None):
                 setattr(cls, self.attname, self.descriptor_class(self))
         if self.choices is not None:
-            if not hasattr(cls, 'get_%s_display' % self.name):
+            if not hasattr(cls, 'get_%s_display' % self.name) or self.choices is not None:
                 setattr(
                     cls,
                     'get_%s_display' % self.name,
```
**变异语义**：追加 `or self.choices is not None`，形成恒真条件。由于此代码已在 `if self.choices is not None:` 块内，`self.choices is not None` 永远为 True，`or True` 使整个条件恒为 True，等价于删除了 `not hasattr` 保护。外表上看是一个"防御性附加检查"，实为引入了短路求值导致的逻辑漏洞。普通用法无法察觉，只在 `test_overriding_FIELD_display` 类型测试下失败。

---

### Group C — 替换
**原 mutation**：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index fe550169bd..772100a25b 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -763,7 +763,7 @@ class Field(RegisterLookupMixin):
             if not getattr(cls, self.attname, None):
                 setattr(cls, self.attname, self.descriptor_class(self))
         if self.choices is not None:
-            if not hasattr(cls, 'get_%s_display' % self.name):
+            if True:  # BUG: removed hasattr check, breaks override
```
**分类**：🔴 必须替换

**理由**：注释中直接包含 `# BUG`，明显人工痕迹，代码审查中会立即被识别为刻意引入的缺陷。

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index fe550169bd..e4d697ae56 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -763,7 +763,7 @@ class Field(RegisterLookupMixin):
             if not getattr(cls, self.attname, None):
                 setattr(cls, self.attname, self.descriptor_class(self))
         if self.choices is not None:
-            if not hasattr(cls, 'get_%s_display' % self.name):
+            if not hasattr(cls, 'get_%s_display' % self.name) or not self.primary_key:
                 setattr(
                     cls,
                     'get_%s_display' % self.name,
```
**变异语义**：追加 `or not self.primary_key`。`self.primary_key` 对于 99% 的字段（包括 choices 字段）为 False，因此 `not self.primary_key` 几乎恒为 True，整个 `or` 条件恒为 True。表面上看像是"主键字段的特殊处理逻辑"，实为近似恒真条件。开发者可能认为主键字段的展示方法需要特殊保护，但这个逻辑推理是错误的。仅在有重写需求的测试（`test_overriding_FIELD_display`）下失败。

---

### Group D — 替换
**原 mutation**：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index fe550169bd..8d4f3ae078 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -763,7 +763,7 @@ class Field(RegisterLookupMixin):
             if not getattr(cls, self.attname, None):
                 setattr(cls, self.attname, self.descriptor_class(self))
         if self.choices is not None:
-            if not hasattr(cls, 'get_%s_display' % self.name):
+            if not hasattr(cls, '_get_%s_display' % self.name):
```
**分类**：🔴 必须替换

**理由**：检查 `_get_foo_bar_display` 而非 `get_foo_bar_display`。Django 使用 `_get_FIELD_display`（无字段名），从不在模型上注册 `_get_foo_bar_display`，故该属性永远不存在，条件恒为 True，等价于无条件执行 setattr。是对基础代码行为的功能等价还原（直接冗余）。

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index fe550169bd..4740a03bed 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -763,7 +763,7 @@ class Field(RegisterLookupMixin):
             if not getattr(cls, self.attname, None):
                 setattr(cls, self.attname, self.descriptor_class(self))
         if self.choices is not None:
-            if not hasattr(cls, 'get_%s_display' % self.name):
+            if not hasattr(cls.__mro__[-1], 'get_%s_display' % self.name):
                 setattr(
                     cls,
                     'get_%s_display' % self.name,
```
**变异语义**：将检查目标改为 `cls.__mro__[-1]`，即 MRO 链末尾的 `object` 类。`object` 永远不拥有 `get_foo_bar_display`，条件恒为 True，始终覆盖用户方法。开发者可能意图通过 MRO 检查"整个继承链都没有该方法时才设置"，但错误地选择了 MRO 尾端而非 `cls` 本身。与 Group A 的 `__bases__[0]` 类似但更隐蔽——`__mro__[-1]` 看起来更像是"彻底检查继承链"的意图。

---

### Group E — 替换
**原 mutation**：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index fe550169bd..5ae925bd39 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -763,7 +763,7 @@ class Field(RegisterLookupMixin):
             if not getattr(cls, self.attname, None):
                 setattr(cls, self.attname, self.descriptor_class(self))
         if self.choices is not None:
-            if not hasattr(cls, 'get_%s_display' % self.name):
+            if True:  # Always create display method
```
**分类**：🔴 必须替换

**理由**：`if True:` 是死代码结构，任何代码审查者都会立即识别为无意义条件，不符合自然代码风格。

**最终 mutation**：
```diff
diff --git a/django/db/models/fields/__init__.py b/django/db/models/fields/__init__.py
index fe550169bd..410e2f2f08 100644
--- a/django/db/models/fields/__init__.py
+++ b/django/db/models/fields/__init__.py
@@ -763,7 +763,7 @@ class Field(RegisterLookupMixin):
             if not getattr(cls, self.attname, None):
                 setattr(cls, self.attname, self.descriptor_class(self))
         if self.choices is not None:
-            if not hasattr(cls, 'get_%s_display' % self.name):
+            if not hasattr(type(cls), 'get_%s_display' % self.name):
                 setattr(
                     cls,
                     'get_%s_display' % self.name,
```
**变异语义**：将检查目标从 `cls`（模型类）改为 `type(cls)`（元类，即 `ModelBase` 或 `type`）。元类不持有任何字段特定的 `get_foo_bar_display` 方法，故条件恒为 True，Django 始终覆盖用户方法。开发者可能混淆了 Python 的 `type(obj)` 语义，在元编程场景中将类和元类弄混。这种错误在不熟悉 Django 元类体系的开发者中非常自然，同时几乎不可能通过阅读代码立即发现（需要深入理解 `type(cls)` 在此上下文的含义）。

---

## 新设计 Mutation 说明

**所有5个 mutation 的共同基础分析**：

- golden patch 的核心修复位于 `contribute_to_class` 的 `if not hasattr(cls, 'get_%s_display' % self.name):` 条件判断
- 该条件的关键要素是：检查目标必须是 `cls` 本身，字符串必须是 `'get_%s_display' % self.name`
- 任何使该条件从"依赖 cls 的属性存在性"变为"近似恒真"的改动都会重现 bug

**设计原则**：

1. **A（`cls.__bases__[0]`）**：利用 Python 继承语义混淆，将"类自身的方法检查"偏移到"直接父类的方法检查"。自然错误原因：开发者认为应防止从父类继承的方法被覆盖。
2. **B（`or self.choices is not None`）**：利用条件短路和上下文冗余，构造不引人注意的恒真条件。自然错误原因：开发者认为"有 choices 时才覆盖"是额外的防护条件，忽略了已在 if 块内。
3. **C（`or not self.primary_key`）**：利用字段属性的统计特性构造近似恒真条件。自然错误原因：开发者认为主键字段不需要保护（虽逻辑混乱但写法自然）。
4. **D（`cls.__mro__[-1]`）**：利用 MRO 遍历理解偏差，检查链尾 `object` 而非 cls。自然错误原因：开发者想用 MRO 确保"整个链上都没有"，但取了错误端。
5. **E（`type(cls)`）**：利用 Python 元类系统的复杂性，混淆类与元类。自然错误原因：不熟悉元编程的开发者将 `type(cls)` 误以为是在检查"类的类型信息"，实为检查元类实例。
