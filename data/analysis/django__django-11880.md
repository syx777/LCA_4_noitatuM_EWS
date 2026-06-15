# django__django-11880

## 问题背景

Django 的 `Field.__deepcopy__` 方法在执行深拷贝时，对 `error_messages` 字典仅做了浅拷贝（通过 `copy.copy(self)`），导致原始字段与所有拷贝实例共享同一个 `error_messages` 字典对象。当 `BaseForm.__init__` 调用 `copy.deepcopy(self.base_fields)` 为每个表单实例生成独立字段时，这些字段的 `error_messages` 仍然是同一个字典引用——对其中任何一个实例的错误消息进行修改，会立即反映到所有其他实例上。

Golden patch 在 `Field.__deepcopy__` 中添加了一行：`result.error_messages = self.error_messages.copy()`，在每次深拷贝时创建一个独立的 `error_messages` 字典副本。

## Golden Patch 语义分析

修复的核心逻辑：`copy.copy(self)` 只做浅拷贝，这意味着 `result.error_messages` 初始时是 `self.error_messages` 的同一个对象引用。`__deepcopy__` 方法已经对 `widget` 进行了显式的深拷贝处理，对 `validators` 进行了显式的列表切片（创建新列表），但漏掉了 `error_messages`。Golden patch 通过 `self.error_messages.copy()` 创建字典的浅拷贝（对于字符串值的字典这足够了，因为字符串是不可变的），确保每个字段拷贝持有独立的错误消息字典，不再与原始字段或其他拷贝共享。

## 调用链分析

```
BaseForm.__init__()
  └─ copy.deepcopy(self.base_fields)        # forms.py:87
       └─ Field.__deepcopy__(self, memo)     # fields.py:198
            ├─ copy.copy(self)               # 浅拷贝，error_messages 仍是同一引用
            ├─ result.widget = copy.deepcopy(self.widget, memo)
            ├─ result.error_messages = self.error_messages.copy()  # [golden fix]
            └─ result.validators = self.validators[:]

Field.validate(value)
  └─ raises ValidationError(self.error_messages['required'])  # fields.py:125

Field.run_validators(value)
  └─ e.message = self.error_messages[e.code]  # fields.py:135
```

`error_messages` 字典在 `Field.__init__` 中通过遍历 MRO 合并各级 `default_error_messages` 并覆盖传入的自定义消息来构建，存储在实例属性 `self.error_messages` 中。每次表单实例化时，`BaseForm.__init__` 调用 `copy.deepcopy` 产生字段拷贝，拷贝后的字段在 `validate()` 和 `run_validators()` 中读取 `self.error_messages`。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 新设计 | 新建 | 原数据集中 A 组不存在，需全新设计 |
| B | 🔴 必须替换 | 替换 | 等价于直接还原 golden patch 的逆操作（条件永远为 False，从不 copy） |
| C | 新设计 | 新建 | 原数据集中 C 组不存在，需全新设计 |
| D | 新设计 | 新建 | 原数据集中 D 组不存在，需全新设计 |
| E | 新设计 | 新建 | 原数据集中 E 组不存在，需全新设计 |

语义浅层共 0 个（已存在的唯一 B 组属于必须替换），全部为新设计。

## 各组 Mutation 分析

### Group A — 替换（新设计）
**原 mutation**：不存在

**分类**：新设计（A1 — Alter Parameter Default or Semantics）

**理由**：利用 deepcopy 的 `memo` 参数语义，模拟开发者"正确使用 memo 优化"但逻辑反转的错误。

**最终 mutation**：
```diff
diff --git a/django/forms/fields.py b/django/forms/fields.py
index 36ec634929..51ac532af4 100644
--- a/django/forms/fields.py
+++ b/django/forms/fields.py
@@ -199,7 +199,7 @@ class Field:
         result = copy.copy(self)
         memo[id(self)] = result
         result.widget = copy.deepcopy(self.widget, memo)
-        result.error_messages = self.error_messages.copy()
+        result.error_messages = memo.get(id(self.error_messages), self.error_messages)
         result.validators = self.validators[:]
         return result
```

**变异语义**：`memo.get(id(self.error_messages), self.error_messages)` 看起来是正确的 deepcopy 备忘录模式——"如果已经拷贝过就用缓存版本，否则用原始"。但正确实现应该是"如果已拷贝过就用缓存，否则**创建新副本**"。由于 `id(self.error_messages)` 通常不在 memo 中，默认总是使用 `self.error_messages`（原始引用，无复制）。代码审查者看到这行会认为这是合理的 memo 优化，不会察觉缺少 `.copy()` 调用。只有在检测字典对象身份的测试 (`assertIsNot`) 下才会失败。

---

### Group B — 替换（必须替换原有 mutation）
**原 mutation**：
```diff
@@ -199,7 +199,8 @@ class Field:
         result = copy.copy(self)
         memo[id(self)] = result
         result.widget = copy.deepcopy(self.widget, memo)
-        result.error_messages = self.error_messages.copy()
+        if not self.error_messages:
+            result.error_messages = self.error_messages.copy()
```

**分类**：🔴 必须替换

**理由**：`self.error_messages` 在 `__init__` 中至少包含 `'required'` 键，永远为非空（truthy），导致条件 `if not self.error_messages` 永远为 False。该 mutation 完全等价于直接还原 golden patch，是对修复的逆操作，属于"功能等价冗余"。

**最终 mutation**（替换）：
```diff
diff --git a/django/forms/fields.py b/django/forms/fields.py
index 36ec634929..8bf2e0feb2 100644
--- a/django/forms/fields.py
+++ b/django/forms/fields.py
@@ -199,7 +199,11 @@ class Field:
         result = copy.copy(self)
         memo[id(self)] = result
         result.widget = copy.deepcopy(self.widget, memo)
-        result.error_messages = self.error_messages.copy()
+        result.error_messages = (
+            self.error_messages.copy()
+            if self.error_messages is not result.error_messages
+            else self.error_messages
+        )
         result.validators = self.validators[:]
         return result
```

**变异语义**：B3（布尔逻辑反转）。开发者写了"如果源和目标已经是不同对象，才 copy；如果是同一对象，则直接使用"。这看起来像是"避免不必要的复制"的防御性代码。但 `copy.copy(self)` 执行后 `result.error_messages is self.error_messages` = True（浅拷贝共享引用），所以 `is not` = False，实际总走 `else self.error_messages`（无复制）。逻辑应该反过来——恰恰在它们 **是** 同一对象时才需要创建副本。这个错误在代码审查中很难发现，因为"避免重复复制"的意图听起来合理。

---

### Group C — 替换（新设计）
**原 mutation**：不存在

**分类**：新设计（C1 — Break Implicit Type Coercion）

**理由**：模拟开发者为处理"非 dict 类型的 error_messages"而添加的类型检查，但遗漏了正常 dict 情况下的复制逻辑。

**最终 mutation**：
```diff
diff --git a/django/forms/fields.py b/django/forms/fields.py
index 36ec634929..e1ea2b7731 100644
--- a/django/forms/fields.py
+++ b/django/forms/fields.py
@@ -199,7 +199,11 @@ class Field:
         result = copy.copy(self)
         memo[id(self)] = result
         result.widget = copy.deepcopy(self.widget, memo)
-        result.error_messages = self.error_messages.copy()
+        result.error_messages = (
+            dict(self.error_messages)
+            if not isinstance(self.error_messages, dict)
+            else self.error_messages
+        )
         result.validators = self.validators[:]
         return result
```

**变异语义**：C1（破坏隐式类型强制）。条件 `not isinstance(self.error_messages, dict)` 永远为 False（因为 `__init__` 总是创建 `dict` 类型的 `error_messages`），所以总走 `else self.error_messages`（无复制）。代码看起来像是开发者在做防御性类型处理——"如果是非标准类型就转换，否则直接使用"。审查者可能认为这是向后兼容的类型安全代码，不会注意到正常路径没有做任何复制。

---

### Group D — 替换（新设计）
**原 mutation**：不存在

**分类**：新设计（D2 — Break Method Idempotency）

**理由**：深拷贝后将源字段的 `error_messages` 赋值为拷贝版本，导致源和拷贝共享同一字典，破坏深拷贝的隔离语义。

**最终 mutation**：
```diff
diff --git a/django/forms/fields.py b/django/forms/fields.py
index 36ec634929..a8fd8eebce 100644
--- a/django/forms/fields.py
+++ b/django/forms/fields.py
@@ -200,6 +200,7 @@ class Field:
         memo[id(self)] = result
         result.widget = copy.deepcopy(self.widget, memo)
         result.error_messages = self.error_messages.copy()
+        self.error_messages = result.error_messages
         result.validators = self.validators[:]
         return result
```

**变异语义**：D2（破坏方法幂等性）。`result.error_messages = self.error_messages.copy()` 创建了独立副本，但随后 `self.error_messages = result.error_messages` 将源字段的 `error_messages` 指向同一个副本，使源字段与拷贝共享同一字典。测试执行 `copy.deepcopy(field)` 后，`field.error_messages` 和 `field_copy.error_messages` 是同一对象，`assertIsNot` 失败。这个 mutation 特别隐蔽，因为它"看起来像"开发者在保持源字段状态一致性（"让源也指向新的 copy"），审查者可能会误以为这是一个状态同步操作。

---

### Group E — 替换（新设计）
**原 mutation**：不存在

**分类**：新设计（E2 — Implicit → Explicit Parameter）

**理由**：将隐式的"始终复制 error_messages"行为改为需要显式参数控制，且默认值为 `False`（不复制），破坏了调用者的隐式假设。

**最终 mutation**：
```diff
diff --git a/django/forms/fields.py b/django/forms/fields.py
index 36ec634929..ab6c905a6e 100644
--- a/django/forms/fields.py
+++ b/django/forms/fields.py
@@ -195,11 +195,12 @@ class Field:
         """
         return BoundField(form, self, field_name)
 
-    def __deepcopy__(self, memo):
+    def __deepcopy__(self, memo, copy_messages=False):
         result = copy.copy(self)
         memo[id(self)] = result
         result.widget = copy.deepcopy(self.widget, memo)
-        result.error_messages = self.error_messages.copy()
+        if copy_messages:
+            result.error_messages = self.error_messages.copy()
         result.validators = self.validators[:]
         return result
 
@@ -765,7 +766,7 @@ class ChoiceField(Field):
         super().__init__(**kwargs)
         self.choices = choices
 
-    def __deepcopy__(self, memo):
+    def __deepcopy__(self, memo, copy_messages=False):
         result = super().__deepcopy__(memo)
         result._choices = copy.deepcopy(self._choices, memo)
         return result
@@ -983,7 +984,7 @@ class MultiValueField(Field):
                 f.required = False
         self.fields = fields
 
-    def __deepcopy__(self, memo):
+    def __deepcopy__(self, memo, copy_messages=False):
         result = super().__deepcopy__(memo)
         result.fields = tuple(x.__deepcopy__(memo) for x in self.fields)
         return result
```

**变异语义**：E2（隐式行为变为显式参数，默认值错误）。`copy.deepcopy(field)` 调用 `field.__deepcopy__(memo)` 时不会传入 `copy_messages`，因此默认 `copy_messages=False`，`error_messages` 不被复制。代码看起来像是开发者在做"可配置的优化"——允许调用者选择是否复制 error_messages。子类 `ChoiceField` 和 `MultiValueField` 的签名也一致更新，看起来很完整。审查者可能会认为默认 `False` 是"为了向后兼容"或"性能优化"，不会意识到这破坏了所有不传参数的标准 `copy.deepcopy` 调用。

## 新设计 Mutation 说明

**所有5个 mutation 均为新设计**（原数据集只有 Group B 一个条目，且该条目必须替换）：

- **A (A1)**：基于对 Python deepcopy `memo` 参数语义的深入理解设计。`memo` 字典的标准用法是 "先查缓存，未命中则创建"，这个 mutation 模拟了开发者理解了"查缓存"但忘记了"未命中时应创建新副本而非使用原始引用"的错误。修改位置精确在 golden fix 行。

- **B (B3)**：基于对 `copy.copy()` 浅拷贝特性的理解。浅拷贝后 `result.error_messages is self.error_messages` = True，开发者可能写了"只有当它们不同时才复制"的防御逻辑，但这与实际需求完全相反。

- **C (C1)**：模拟开发者为潜在的非标准 `error_messages` 类型添加了类型安全检查，但用 `isinstance(x, dict)` 的分支反而遗漏了 dict 类型的复制。这是典型的"处理了异常路径，忘记了正常路径"的错误。

- **D (D2)**：基于对 deepcopy 语义的误解——开发者可能认为 deepcopy 应该让源和拷贝"同步到同一个新版本"，通过 `self.error_messages = result.error_messages` 实现，但这恰好破坏了隔离。这个 mutation 在整个 `__deepcopy__` 方法外加一行，审查者容易忽略。

- **E (E2)**：基于对 Python dunder 方法扩展接口的理解。`__deepcopy__(self, memo)` 是标准签名，但 Python 协议允许额外的关键字参数。添加 `copy_messages=False` 参数看起来是合理的 API 扩展，但默认值使得所有标准 `copy.deepcopy` 调用都不复制 error_messages。同时一致地更新了所有子类的 `__deepcopy__` 签名，使这个改动看起来更像是有意为之的重构。
