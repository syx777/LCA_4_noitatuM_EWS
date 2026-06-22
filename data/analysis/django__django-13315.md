# django__django-13315

## 问题背景

在 Django 的 `ForeignKey`/`ManyToManyField` 中，若 `limit_choices_to` 为一个 Q 对象且涉及 JOIN（如跨关联关系的过滤），`apply_limit_choices_to_to_formfield` 会使用 `complex_filter` 直接过滤 queryset。由于一对多关系的 JOIN 会产生重复行，最终表单字段的 choices 中出现重复选项。

Golden patch 的修复方案：将 `queryset.complex_filter(limit_choices_to)` 改为基于 `Exists` 子查询的过滤，通过相关子查询（correlated subquery）确保每个外部行最多匹配一次，消除重复。

## Golden Patch 语义分析

修复核心逻辑：

1. **引入本地导入**：在函数内部引入 `Exists, OuterRef, Q`
2. **dict → Q 转换**：若 `limit_choices_to` 是 dict，先转为 `Q(**limit_choices_to)`
3. **关联条件**：`complex_filter &= Q(pk=OuterRef('pk'))` — 将外部查询的 pk 作为子查询的额外过滤条件，建立相关子查询
4. **Exists 包装**：`Exists(model._base_manager.filter(complex_filter))` — 对每个外部行检查"是否存在满足 complex_filter 且 pk=外部行pk 的对象"
5. **结果**：每个外部行最多通过一次 EXISTS 检查，消除 JOIN 导致的重复

关键语义：**相关子查询 + Exists** 是消除重复的核心，而不是简单的 `.distinct()`。`_base_manager` 用于绕过自定义 manager 的额外过滤，确保子查询能找到正确的对象。

## 调用链分析

```
ModelChoiceField.__init__() / ModelMultipleChoiceField.__init__()
  └── self.limit_choices_to = limit_choices_to
  └── self.get_limit_choices_to()
        └── [callable invocation if callable]
        └── return self.limit_choices_to

BaseModelForm.__init__()
  └── for formfield in self.fields.values():
        apply_limit_choices_to_to_formfield(formfield)

fields_for_model()
  └── apply_limit_choices_to_to_formfield(formfield)  [if apply_limit_choices_to=True]

apply_limit_choices_to_to_formfield(formfield)
  └── formfield.get_limit_choices_to()  → limit_choices_to (Q or dict)
  └── [dict → Q conversion]
  └── complex_filter &= Q(pk=OuterRef('pk'))  ← correlation
  └── formfield.queryset.filter(
          Exists(formfield.queryset.model._base_manager.filter(complex_filter))
      )
```

数据流：`limit_choices_to`（来自 model field 定义）→ form field → `get_limit_choices_to()`（处理 callable）→ 转换为 Q → 与相关条件组合 → 过滤 queryset

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 缺失 | 新增 | mutations.jsonl 中无 Group A，需新设计 |
| B | 语义浅层（B3 单行反转） | 保留 | 修改位置在关键逻辑节点，模拟真实的 isinstance 判断方向失误 |
| C | 🔴 必须替换 | 替换 | 原 mutation 直接删除 isinstance 检查，导致 dict &= Q() 崩溃，不自然 |
| D | 🔴 必须替换 | 替换 | 原 mutation 将 queryset 传给 filter()，运行时 FieldError，不自然 |
| E | 🔴 必须替换 | 替换 | 使用虚构属性 `_use_distinct`，明显人工痕迹 |

语义浅层共 1 个（B），floor(1/2)=0 个需替换语义浅层，保留 B。

## 各组 Mutation 分析

### Group A — 新增

**原 mutation**：（缺失）

**分类**：新增

**理由**：mutations.jsonl 中无 Group A，需按 A1（Alter Parameter Default or Semantics）策略设计。

**最终 mutation**：

```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index 0591cdf338..64bd25df96 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -1222,8 +1222,6 @@ class ModelChoiceField(ChoiceField):
 
         If it is a callable, invoke it and return the result.
         """
-        if callable(self.limit_choices_to):
-            return self.limit_choices_to()
         return self.limit_choices_to
 
     def __deepcopy__(self, memo):
```

**变异语义**：`get_limit_choices_to()` 不再调用 callable，直接返回 `self.limit_choices_to`。当 `limit_choices_to` 是 callable 时，返回 callable 对象本身而非其结果。后续 `if limit_choices_to:` → True（callable 是真值），然后 `not isinstance(callable, Q)` → True，执行 `Q(**callable)` → TypeError。所有使用 callable 形式的 `limit_choices_to` 都会在表单实例化时崩溃。此 mutation 在不了解 callable 调用语义的情况下很难发现——代码看起来只是"少了一个 if"，而且 `return self.limit_choices_to` 语句本身是合法的。

---

### Group B — 保留

**原 mutation**：

```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index 0591cdf338..5b523b7aba 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -102,7 +102,7 @@ def apply_limit_choices_to_to_formfield(formfield):
         limit_choices_to = formfield.get_limit_choices_to()
         if limit_choices_to:
             complex_filter = limit_choices_to
-            if not isinstance(complex_filter, Q):
+            if isinstance(complex_filter, Q):
                 complex_filter = Q(**limit_choices_to)
             complex_filter &= Q(pk=OuterRef('pk'))
             # Use Exists() to avoid potential duplicates.
```

**分类**：🟡 语义浅层（B3 反转布尔逻辑）— 保留

**理由**：`not isinstance → isinstance` 颠倒了 dict 转换的方向——当 `limit_choices_to` 是 Q 对象时才做 `Q(**...)` 转换（反而对已经是 Q 的对象做 `Q(**Q_obj)` 导致错误），而 dict 输入则跳过转换直接 `dict &= Q(...)` 崩溃。修改位置在关键逻辑节点（类型分发判断处），能模拟真实开发者对 isinstance 方向的判断失误。

**变异语义**：当 `limit_choices_to` 是 dict 时跳过转换，dict 直接参与 `&=` 操作导致 TypeError；当是 Q 时错误地重新包装为 `Q(**Q_obj)`（Q 不是 Mapping，会报 TypeError）。两个分支都失败，但在不涉及 JOIN 的简单测试下不触发此路径。

---

### Group C — 替换

**原 mutation**：

```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index 0591cdf338..4b040ebc1b 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -102,8 +102,6 @@ def apply_limit_choices_to_to_formfield(formfield):
         limit_choices_to = formfield.get_limit_choices_to()
         if limit_choices_to:
             complex_filter = limit_choices_to
-            if not isinstance(complex_filter, Q):
-                complex_filter = Q(**limit_choices_to)
             complex_filter &= Q(pk=OuterRef('pk'))
             # Use Exists() to avoid potential duplicates.
```

**分类**：🔴 必须替换

**理由**：删除 isinstance 检查后，当 `limit_choices_to` 为 dict 时，`dict &= Q(pk=OuterRef('pk'))` 会抛出 TypeError（dict 不支持 `&=` Q 对象），导致明显崩溃而非隐秘的行为错误。不够自然，审查时立即发现。

**最终 mutation**：

```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index 0591cdf338..64a21b4350 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -103,7 +103,7 @@ def apply_limit_choices_to_to_formfield(formfield):
         if limit_choices_to:
             complex_filter = limit_choices_to
             if not isinstance(complex_filter, Q):
-                complex_filter = Q(**limit_choices_to)
+                complex_filter = Q(*limit_choices_to)
             complex_filter &= Q(pk=OuterRef('pk'))
             # Use Exists() to avoid potential duplicates.
             formfield.queryset = formfield.queryset.filter(
```

**变异语义**：将 `Q(**limit_choices_to)`（关键字解包 dict）改为 `Q(*limit_choices_to)`（位置参数解包 dict keys）。`Q(*{'k': 'v'})` 等价于 `Q('k')`，但 Q 对象的位置参数期望是 Q/Node 对象，传入字符串会导致 TypeError 或 FieldError。此 mutation 只影响 `limit_choices_to` 为 dict 的情况（Q 情况正常）。`*` vs `**` 是非常难以肉眼发现的差异，代码逻辑结构完全正常。

---

### Group D — 替换

**原 mutation**：

```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index 0591cdf338..128acbffc6 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -107,7 +107,7 @@ def apply_limit_choices_to_to_formfield(formfield):
             complex_filter &= Q(pk=OuterRef('pk'))
             # Use Exists() to avoid potential duplicates.
             formfield.queryset = formfield.queryset.filter(
-                Exists(formfield.queryset.model._base_manager.filter(complex_filter)),
+                formfield.queryset.model._base_manager.filter(complex_filter),
             )
```

**分类**：🔴 必须替换

**理由**：将 queryset 对象直接传入 `.filter()` 作为位置参数，Django 会在查询构造时抛出 FieldError（无法将 QuerySet 解析为过滤条件），行为过于破坏性，不自然。

**最终 mutation**：

```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index 0591cdf338..923e33a696 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -104,7 +104,7 @@ def apply_limit_choices_to_to_formfield(formfield):
             complex_filter = limit_choices_to
             if not isinstance(complex_filter, Q):
                 complex_filter = Q(**limit_choices_to)
-            complex_filter &= Q(pk=OuterRef('pk'))
+            complex_filter |= Q(pk=OuterRef('pk'))
             # Use Exists() to avoid potential duplicates.
             formfield.queryset = formfield.queryset.filter(
                 Exists(formfield.queryset.model._base_manager.filter(complex_filter)),
```

**变异语义**：将 `&=`（AND）改为 `|=`（OR）。原逻辑：`complex_filter AND pk=outer.pk` — 精确关联子查询。变异后：`complex_filter OR pk=outer.pk` — 对任意外部行 X，`pk=X.pk` 条件在子查询中始终能匹配该行本身（`_base_manager.filter(... OR pk=X.pk)` 中，由于 OuterRef 的值就是当前行的 pk，子查询始终找到至少一行），导致 `EXISTS(...)` 对所有外部行都为 True，最终返回所有 Character（无过滤）。代码结构合理，`|` 和 `&` 的区别在不深入理解相关子查询逻辑时极难发现。

---

### Group E — 替换

**原 mutation**：

```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index 0591cdf338..002b54bd0a 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -104,11 +104,14 @@ def apply_limit_choices_to_to_formfield(formfield):
             complex_filter = limit_choices_to
             if not isinstance(complex_filter, Q):
                 complex_filter = Q(**limit_choices_to)
-            complex_filter &= Q(pk=OuterRef('pk'))
-            # Use Exists() to avoid potential duplicates.
-            formfield.queryset = formfield.queryset.filter(
-                Exists(formfield.queryset.model._base_manager.filter(complex_filter)),
-            )
+            if getattr(formfield, "_use_distinct", False):
+                complex_filter &= Q(pk=OuterRef('pk'))
+                # Use Exists() to avoid potential duplicates.
+                formfield.queryset = formfield.queryset.filter(
+                    Exists(formfield.queryset.model._base_manager.filter(complex_filter)),
+                )
+            else:
+                formfield.queryset = formfield.queryset.filter(complex_filter)
```

**分类**：🔴 必须替换

**理由**：使用虚构属性 `_use_distinct` 作为开关，明显人工痕迹，代码审查立即发现。

**最终 mutation**：

```diff
diff --git a/django/forms/models.py b/django/forms/models.py
index 0591cdf338..157a4e2abe 100644
--- a/django/forms/models.py
+++ b/django/forms/models.py
@@ -104,7 +104,6 @@ def apply_limit_choices_to_to_formfield(formfield):
             complex_filter = limit_choices_to
             if not isinstance(complex_filter, Q):
                 complex_filter = Q(**limit_choices_to)
-            complex_filter &= Q(pk=OuterRef('pk'))
             # Use Exists() to avoid potential duplicates.
             formfield.queryset = formfield.queryset.filter(
                 Exists(formfield.queryset.model._base_manager.filter(complex_filter)),
```

**变异语义**：删除 `complex_filter &= Q(pk=OuterRef('pk'))` 这一关联条件，使 Exists 子查询变为**非相关子查询**（uncorrelated subquery）。原逻辑：对每个外部行 X，检查是否存在同时满足 `complex_filter AND pk=X.pk` 的对象（精确过滤）。变异后：子查询只检查"是否存在任意满足 complex_filter 的对象"，与外部行无关。若测试数据中有任何字符匹配 `complex_filter`，则所有外部行都通过 EXISTS 检查，返回全部 Character 而非正确的子集。代码外观合理（保留了 Exists 和 _base_manager），注释也还在，仅少了一行关联条件，极难发现。

## 新设计 Mutation 说明

### Group A（A1 策略）
- **代码分析基础**：`ModelChoiceField.get_limit_choices_to()` 是 callable 处理的唯一入口。在 `apply_limit_choices_to_to_formfield` 调用之前，所有 callable 形式的 `limit_choices_to` 必须在此处被展开为 Q 或 dict。
- **选择位置原因**：此 mutation 的修改位置（`get_limit_choices_to`）与其他4个 mutation 的位置（`apply_limit_choices_to_to_formfield`）完全不同，避免了5个 mutation 集中在同一函数。且此处的 bug 在调用链上游，效果在下游才显现。
- **模拟的开发者错误**：开发者可能认为"直接返回 `limit_choices_to` 属性就够了，调用方会处理 callable"——对 callable 处理职责归属产生误解。

### Group C（C1 策略）
- **代码分析基础**：`Q(**dict)` 是将字典转为 Q 条件的标准方式，`*` vs `**` 是关键区别。
- **选择位置原因**：只修改 dict-to-Q 转换路径，Q 路径不受影响，错误只在 `limit_choices_to` 为 dict 时触发。
- **模拟的开发者错误**：在重构或快速编写时，`*` 和 `**` 混淆是常见笔误，代码视觉上几乎相同。

### Group D（B3 策略）
- **代码分析基础**：相关子查询的核心在于 `&=` 将外部行 pk 加入子查询过滤条件。`|=` 看似只是运算符变化，但语义上彻底破坏了相关性。
- **选择位置原因**：与 B 的修改位置不同（B 改的是 isinstance 判断，D 改的是 Q 组合运算符）。
- **模拟的开发者错误**：在理解相关子查询时，对 AND/OR 语义理解错误——认为"只要满足其中一个条件就能找到对象"。

### Group E（D3 策略 — 移除前置步骤）
- **代码分析基础**：`complex_filter &= Q(pk=OuterRef('pk'))` 是 Exists 子查询能正确工作的**前提**——没有它，Exists 失去相关性，成为全局检查。
- **选择位置原因**：修改位置与 B/C/D 不同。仅删除一行，代码变得更简洁，反而看起来像是"优化"。
- **模拟的开发者错误**：开发者可能认为 Exists 中的 `complex_filter` 本身已经能正确过滤，不需要额外的 pk 关联条件，从而"简化"了代码。
