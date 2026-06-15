# django__django-12406

## 问题背景

当 ModelForm 中外键字段使用 `RadioSelect` 小部件时，即使模型字段设置了 `blank=False`（不允许为空），Django 仍然会在选项列表中显示一个空白选项（"-------"）。这与 `SelectWidget` 的行为不同——对于 RadioSelect，显示空白选项在语义上是多余的，因为该控件本身已通过"未选择任何项"的视觉状态表达了"未填写"的含义。

Golden patch 做了两件事：
1. 在 `ForeignKey.formfield()` 中，将模型字段的 `blank` 属性传递给 `ModelChoiceField`（`'blank': self.blank`）
2. 在 `ModelChoiceField.__init__()` 中，接受新的 `blank` 参数，并在 `Field.__init__()` 调用之后（widget 已解析）检查：当 widget 是 RadioSelect 且 `blank=False` 时，将 `empty_label` 设为 `None`，从而不显示空白选项

## Golden Patch 语义分析

核心修复逻辑：
- **修复前**：`empty_label` 的初始化在 `Field.__init__` 之前执行，此时 `self.widget` 尚未解析（`widget` 参数可能是类而非实例），无法判断是否为 RadioSelect
- **修复后**：将 `empty_label` 的初始化移到 `Field.__init__` 之后，此时 `self.widget` 已被实例化，可以用 `isinstance(self.widget, RadioSelect)` 判断
- 新的条件逻辑：`(required and initial is not None) or (isinstance(self.widget, RadioSelect) and not blank)`
  - 第一个分支：当 `required=True` 且有 `initial` 值时，不显示空选项（原有逻辑）
  - 第二个分支：当使用 RadioSelect 且字段不允许为空时，不显示空选项（新逻辑）

## 调用链分析

```
ForeignKey.formfield()
  └── 传入 blank=self.blank 给 ModelChoiceField.__init__()
        └── Field.__init__()  [解析 widget 类/实例]
              └── self.widget 已为 RadioSelect 实例
        └── 检查条件，设置 self.empty_label
              └── self.queryset = queryset  [触发 _set_queryset]
                    └── self.widget.choices = self.choices  [触发 _get_choices]
                          └── ModelChoiceIterator(self)
                                └── __iter__: 根据 empty_label 决定是否 yield ("", ...)
```

数据流：`ForeignKey.blank → ModelChoiceField.__init__(blank=) → self.empty_label → ModelChoiceIterator.__iter__ → choices 列表`

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 语义浅层 | 保留 | `not blank` → `blank`，逻辑取反，是唯一的语义浅层，floor(1/2)=0 个需替换 |
| B | 必须替换 | 替换 | `empty_label is not None` → `is None`，直接反转迭代器逻辑，过于明显 |
| C | 新设计 | 替换（新建） | 原数据无 C 组，需新设计 |
| D | 新设计 | 替换（新建） | 原数据无 D 组，需新设计 |
| E | 必须替换 | 替换 | 添加无效参数 `_check_widget_blank=False` 使 RadioSelect 检查永远不触发，人工痕迹明显 |

语义浅层共 1 个（A），替换其中最弱的 floor(1/2)=0 个：无需替换 A。

## 各组 Mutation 分析

### Group A — 保留
**原 mutation**：
```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index a4d7118cd1..7bebc3189b 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -1193,7 +1193,7 @@ class ModelChoiceField(ChoiceField):
         )
         if (
             (required and initial is not None) or
-            (isinstance(self.widget, RadioSelect) and not blank)
+            (isinstance(self.widget, RadioSelect) and blank)
         ):
             self.empty_label = None
         else:
```
**分类**：🟡 语义浅层（保留）
**理由**：将 `not blank` 改为 `blank`，逻辑完全相反——现在只有 `blank=True` 的 RadioSelect 才会隐藏空选项，而 `blank=False` 的反而显示空选项。虽然是单行反转，但位置在核心逻辑节点，能模拟开发者将条件含义搞反的错误。唯一的语义浅层，floor(1/2)=0 个需替换，保留。
**最终 mutation**（与原相同）：
```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index a4d7118cd1..7bebc3189b 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -1193,7 +1193,7 @@ class ModelChoiceField(ChoiceField):
         )
         if (
             (required and initial is not None) or
-            (isinstance(self.widget, RadioSelect) and not blank)
+            (isinstance(self.widget, RadioSelect) and blank)
         ):
             self.empty_label = None
         else:
```
**变异语义**：反转了 `blank` 的判断，使 RadioSelect + blank=False 的组合不会隐藏空选项，而 blank=True 的反而隐藏。直接失败于 `test_non_blank_foreign_key_with_radio` 测试，但通过大多数非 RadioSelect 路径的测试。

---

### Group B — 替换
**原 mutation**：
```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index a4d7118cd1..cadd4d7df4 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -1146,7 +1146,7 @@ class ModelChoiceIterator:
         self.queryset = field.queryset
 
     def __iter__(self):
-        if self.field.empty_label is not None:
+        if self.field.empty_label is None:
             yield ("", self.field.empty_label)
         queryset = self.queryset
```
**分类**：🔴 必须替换
**理由**：将 `is not None` 改为 `is None`，这样只有当 `empty_label` 为 None 时才 yield 空选项（而 None 表示不需要空选项），逻辑完全颠倒。代码审查者一眼就能发现——在 `yield ("", self.field.empty_label)` 前提是 `empty_label is None`，意味着 yield 的是 `("", None)` 而非有效标签，非常不自然。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/django/db/models/fields/related.py b/django/db/models/fields/related.py
index 17a08fa931..dc04b14292 100644
--- a/django/db/models/fields/related.py
+++ b/django/db/models/fields/related.py
@@ -980,7 +980,7 @@ class ForeignKey(ForeignObject):
             'queryset': self.remote_field.model._default_manager.using(using),
             'to_field_name': self.remote_field.field_name,
             **kwargs,
-            'blank': self.blank,
+            'blank': not self.blank,
         })
 
     def db_check(self, connection):
```
**变异语义**：在数据源头（`ForeignKey.formfield`）将 `blank` 属性取反后传给 `ModelChoiceField`。这意味着：模型中 `blank=False` 的字段会收到 `blank=True`，从而保留空选项；`blank=True` 的字段反而会隐藏空选项。跨文件、跨调用链，代码看起来逻辑合理（传递了 `blank`），只是取反了。会导致 `test_non_blank_foreign_key_with_radio` 和 `test_blank_foreign_key_with_radio` 均失败。

---

### Group C — 替换（新设计）
**原 mutation**：无（原数据中不存在 C 组）

**最终 mutation**：
```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index a4d7118cd1..47c48e0a51 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -1192,7 +1192,7 @@ class ModelChoiceField(ChoiceField):
             initial=initial, help_text=help_text, **kwargs
         )
         if (
-            (required and initial is not None) or
+            (required and initial is not None) and
             (isinstance(self.widget, RadioSelect) and not blank)
         ):
             self.empty_label = None
```
**变异语义**：将条件的 `or` 改为 `and`，使得 `empty_label=None` 只有在两个条件**同时**满足时才生效：既要 `required=True` 且 `initial` 不为空，又要是 RadioSelect 且 `blank=False`。实际上对于典型的 RadioSelect + blank=False 场景（initial=None），第一个条件为 False，整个表达式为 False，`empty_label` 不会被设为 None，空选项仍然显示。这模拟了开发者误将"两个独立触发条件"理解为"需同时满足的复合条件"的逻辑错误。

---

### Group D — 替换（新设计）
**原 mutation**：无（原数据中不存在 D 组）

**最终 mutation**：
```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index a4d7118cd1..3e30b98f38 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -1224,6 +1224,8 @@ class ModelChoiceField(ChoiceField):
 
     def _set_queryset(self, queryset):
         self._queryset = None if queryset is None else queryset.all()
+        if isinstance(self.widget, RadioSelect) and self.empty_label is None:
+            self.empty_label = '---------'
         self.widget.choices = self.choices
 
     queryset = property(_get_queryset, _set_queryset)
```
**变异语义**：利用执行顺序漏洞。`ModelChoiceField.__init__` 中，golden patch 先设好 `empty_label=None`，再执行 `self.queryset = queryset`，后者会触发 `_set_queryset`。此 mutation 在 `_set_queryset` 中检测 RadioSelect + empty_label=None 的状态，并将 empty_label 重置为 `'---------'`，从而悄悄撤销了 golden patch 的修复。代码看起来像在"修复"某个潜在的状态不一致问题，但实际上破坏了 RadioSelect 隐藏空选项的功能。跨方法协调，难以通过代码审查发现。

---

### Group E — 替换
**原 mutation**：
```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index a4d7118cd1..ff304f0182 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -1184,7 +1184,7 @@ class ModelChoiceField(ChoiceField):
     def __init__(self, queryset, *, empty_label="---------",
                  required=True, widget=None, label=None, initial=None,
                  help_text='', to_field_name=None, limit_choices_to=None,
-                 blank=False, **kwargs):
+                 blank=False, _check_widget_blank=False, **kwargs):
         ...
-            (isinstance(self.widget, RadioSelect) and not blank)
+            (_check_widget_blank and isinstance(self.widget, RadioSelect) and not blank)
```
**分类**：🔴 必须替换
**理由**：添加了永远为 False 的参数 `_check_widget_blank=False`，使 RadioSelect 检查变成死代码。人工痕迹明显：下划线开头的内部参数、默认为 False 的布尔开关，代码审查者立即会发现这是无用条件。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index a4d7118cd1..15d6c4fa61 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -1162,7 +1162,7 @@ class ModelChoiceIterator:
         return self.queryset.count() + (1 if self.field.empty_label is not None else 0)
 
     def __bool__(self):
-        return self.field.empty_label is not None or self.queryset.exists()
+        return self.field.empty_label is not None and self.queryset.exists()
 
     def choice(self, obj):
         return (
```
**变异语义**：将 `ModelChoiceIterator.__bool__` 中的 `or` 改为 `and`。对于 RadioSelect + blank=False 字段（`empty_label=None`），`bool(field.choices)` 将返回 False（因为 `None is not None` 为 False），即使 queryset 中有数据。这影响所有依赖 `bool(choices)` 进行判断的地方，包括 `test_choices_bool_empty_label` 测试，以及任何检查字段是否有可用选项的验证逻辑。改变了 queryset 与 empty_label 之间的独立性语义。

## 新设计 Mutation 说明

### Group B（新）：跨文件传参取反
基于对整条调用链的分析：`ForeignKey.blank → formfield() → ModelChoiceField(blank=)` → 在最上游取反。选择这个位置是因为它在不同文件（`related.py`），表面上看修复"正确传递了 blank 参数"，只是多了一个 `not`，而调试者通常先看 `forms/models.py` 中的条件逻辑，不会第一时间去 `fields/related.py` 检查传入值是否被取反。

### Group C（新）：逻辑组合变异
分析 golden patch 的两个分支（`required+initial` 或 `RadioSelect+blank`）是独立的触发条件，选择将 `or` 改为 `and`，模拟开发者将"两个独立允许条件"误理解为"需同时满足的充分条件"。对 initial=None 的典型场景（99% 的 FK 字段无 initial），第一条件必为 False，整个 AND 表达式为 False，bug 总是发生。

### Group D（新）：跨方法状态覆盖
深入分析 `__init__` 的执行顺序：golden patch 在 `Field.__init__()` 后设置 `empty_label`，然后 `self.queryset = queryset` 触发 `_set_queryset`。在 `_set_queryset` 里重置 `empty_label` 是一种"合理防御性编程"的外表，实际上悄悄撤销了修复。这个 mutation 的根因（`_set_queryset`）与症状（choices 仍包含空选项）之间有间接距离，难以追踪。
