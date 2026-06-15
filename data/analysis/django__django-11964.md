# django__django-11964

## 问题背景

Django 的 `TextChoices` / `IntegerChoices` 字段在将枚举成员转换为字符串时，返回的是 `ClassName.MEMBER_NAME`（如 `MyChoice.FIRST_CHOICE`）而非枚举成员的 `.value`（如 `first`）。

根本原因：Python 3.6–3.10 中，`enum.Enum.__str__` 方法返回 `'ClassName.member_name'` 格式，enum 元类会在每个枚举类上显式设置此方法，覆盖 MRO 中 `int.__str__` 或 `str.__str__` 的查找。Django 的 `Choices(enum.Enum)` 没有自定义 `__str__`，因此继承了这一行为。

Golden Patch 的修复：在 `Choices` 类中新增 `__str__` 方法，返回 `str(self.value)`，保证所有 `Choices` 子类（包括 `IntegerChoices` 和 `TextChoices`）在字符串化时返回枚举成员的存储值。

## Golden Patch 语义分析

```diff
class Choices(enum.Enum, metaclass=ChoicesMeta):
    """Class for creating enumerated choices."""
-    pass
+
+    def __str__(self):
+        """
+        Use value when cast to str, so that Choices set as model instance
+        attributes are rendered as expected in templates and similar contexts.
+        """
+        return str(self.value)
```

核心修复：重写 `Choices.__str__`，将枚举成员的字符串化语义从"返回枚举标识符"改为"返回枚举的存储值"。`str(self.value)` 确保无论值的原始类型（int、str、bytes 等），都转换为字符串。

**为什么这样改是正确的**：Django 模型字段将枚举成员的 `.value` 存入数据库；当从数据库读回后，字段值是 Python 原生类型（str 或 int）。如果新建对象时字段是枚举成员，`str()` 调用应返回同样的表示，保证一致性。模板渲染也依赖 `str()` 来展示字段值，故需要 `__str__` 返回 `.value`。

## 调用链分析

```
django/db/models/enums.py:
  ChoicesMeta(enum.EnumMeta).__new__
    └─ 构建 cls._value2label_map_ = {value: label}
    └─ cls.label = property(lambda self: cls._value2label_map_.get(self.value))
  
  Choices(enum.Enum)
    └─ __str__(self) [golden patch 新增]
         └─ return str(self.value)
  
  IntegerChoices(int, Choices)
    └─ 无自定义 __str__，MRO 使用 Choices.__str__
  
  TextChoices(str, Choices)
    └─ 无自定义 __str__，MRO 使用 Choices.__str__
    └─ _generate_next_value_(name, ...) → return name
```

**数据流**：
- 枚举成员创建时，`ChoicesMeta.__new__` 解析 `(value, label)` 二元组，存储 `value` 到 classdict，`label` 到 `_value2label_map_`
- `self.value` 是枚举成员的原始存储值（int 或 str）
- `self.label` 通过 `_value2label_map_[self.value]` 查找人类可读标签（如 'Diamond'）
- `__str__` 的语义是：将枚举成员序列化为其机器值的字符串形式

**被 `__str__` 影响的场景**：模板渲染 `{{ obj.field }}`、API 序列化 `str(member)`、日志、表单提交。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟡 语义浅层（保留） | 保留 | `value→name` 仅返回错误内容，不崩溃，无其他 P2P 测试检查 str() 内容，较难发现 |
| B | 🔴 必须替换 | 替换 | `self.value is None` 对可迭代成员永假，等价于直接还原 bug，典型功能等价冗余 |
| C | 🟡 语义浅层（最弱，替换） | 替换 | `return self.value` 对 IntegerChoices 立即 TypeError，任何 int 枚举使用都会崩溃，过于明显 |
| D | 🔴 必须替换 | 替换 | `_str_initialized` 属性永不存在，dead code 常驻，人工痕迹明显 |
| E | 🔴 必须替换 | 替换 | `_use_value_in_str` 类属性永不设置，等价于直接还原 bug，与 B 类似的虚假 feature flag |

语义浅层共 2 个（A、C），替换其中最弱的 floor(2/2) = 1 个：**替换 C**（TypeError 比错误字符串更易被任意使用 int 枚举的测试捕获）。

加上必须替换的 B、D、E，共替换 4 个。

## 各组 Mutation 分析

### Group A — 保留

**原 mutation**：
```diff
diff --git a/django/db/models/enums.py b/django/db/models/enums.py
index ae20ef6d93..32fdb51e37 100644
--- a/django/db/models/enums.py
+++ b/django/db/models/enums.py
@@ -66,7 +66,7 @@ class Choices(enum.Enum, metaclass=ChoicesMeta):
         Use value when cast to str, so that Choices set as model instance
         attributes are rendered as expected in templates and similar contexts.
         """
-        return str(self.value)
+        return str(self.name)
 
 
 class IntegerChoices(int, Choices):
```
**分类**：🟡 语义浅层（保留）

**理由**：单行修改但位于关键逻辑节点（唯一的 `__str__` 实现），模拟了真实开发者混淆 `name`（成员标识符，如 `'FRESHMAN'`）和 `value`（存储值，如 `'FR'`）的错误。返回值是合法字符串，不会崩溃。当前 P2P 测试中无任何测试检查 `str(member)` 的具体内容，因此只有 `test_str` 能发现此错误。是两个浅层 mutation 中较强的一个（保留）。

**最终 mutation**：（与原相同）
```diff
-        return str(self.value)
+        return str(self.name)
```

**变异语义**：枚举成员的字符串表示返回成员标识符名称（如 `'JET_SKI'`）而非值（如 `'3'`）。所有 15 个 test_str 子测试均失败（所有成员的 name ≠ value），但不抛出异常，代码运行正常。

---

### Group B — 替换

**原 mutation**：
```diff
diff --git a/django/db/models/enums.py b/django/db/models/enums.py
index ae20ef6d93..9e97307c02 100644
--- a/django/db/models/enums.py
+++ b/django/db/models/enums.py
@@ -66,7 +66,9 @@ class Choices(enum.Enum, metaclass=ChoicesMeta):
         Use value when cast to str, so that Choices set as model instance
         attributes are rendered as expected in templates and similar contexts.
         """
-        return str(self.value)
+        if self.value is None:
+            return str(self.value)
+        return super().__str__()
 
 
 class IntegerChoices(int, Choices):
```
**分类**：🔴 必须替换

**理由**：`self.value is None` 仅对 `__empty__` 成员为真，但 `__empty__` 以 `__` 开头，Python enum 不将其纳入常规迭代，`for member in cls` 不会遍历它。因此对所有可迭代成员，永远执行 `return super().__str__()`，等价于调用 `enum.Enum.__str__()` 返回 `'ClassName.MEMBER'`——完全等效于还原修复前的 bug。None 检查是多余的伪条件，人工痕迹明显。

**最终 mutation**（替换）：
```diff
diff --git a/django/db/models/enums.py b/django/db/models/enums.py
index ae20ef6d93..95b7eecba2 100644
--- a/django/db/models/enums.py
+++ b/django/db/models/enums.py
@@ -66,6 +66,8 @@ class Choices(enum.Enum, metaclass=ChoicesMeta):
         Use value when cast to str, so that Choices set as model instance
         attributes are rendered as expected in templates and similar contexts.
         """
+        if isinstance(self.value, int):
+            return str(self.name)
         return str(self.value)
 
 
```
**变异语义**：对 `IntegerChoices` 成员（Suit、Vehicle）返回枚举名称（如 `'JET_SKI'`）而非整数值（如 `'3'`）；对 `TextChoices` 成员（Gender、YearInSchool）行为正确。模拟真实错误："整数枚举应该展示有意义的名称而非裸数字"。仅影响 IntegerChoices（7 个子测试），TextChoices 测试全部通过，难以被只测试字符串枚举的测试发现。

---

### Group C — 替换

**原 mutation**：
```diff
diff --git a/django/db/models/enums.py b/django/db/models/enums.py
index ae20ef6d93..9cde8bd077 100644
--- a/django/db/models/enums.py
+++ b/django/db/models/enums.py
@@ -66,7 +66,7 @@ class Choices(enum.Enum, metaclass=ChoicesMeta):
         Use value when cast to str, so that Choices set as model instance
         attributes are rendered as expected in templates and similar contexts.
         """
-        return str(self.value)
+        return self.value
 
 
 class IntegerChoices(int, Choices):
```
**分类**：🟡 语义浅层（替换，最弱）

**理由**：对 `TextChoices`（value 已为 str）完全透明，行为不变；对 `IntegerChoices`（value 为 int）直接抛 `TypeError: __str__ returned non-string (type int)`。RuntimeError 比"错误字符串内容"更易被任何使用 IntegerChoices 的代码检测。是两个浅层 mutation 中最弱的，选择替换。

**最终 mutation**（替换）：
```diff
diff --git a/django/db/models/enums.py b/django/db/models/enums.py
index ae20ef6d93..582fa93d6e 100644
--- a/django/db/models/enums.py
+++ b/django/db/models/enums.py
@@ -66,7 +66,7 @@ class Choices(enum.Enum, metaclass=ChoicesMeta):
         Use value when cast to str, so that Choices set as model instance
         attributes are rendered as expected in templates and similar contexts.
         """
-        return str(self.value)
+        return str(self.label) if self.label else str(self.value)
 
 
 class IntegerChoices(int, Choices):
```
**变异语义**：当 `self.label` 非空时返回标签字符串（如 `'Jet Ski'`、`'Male'`、`'Freshman'`）而非值（如 `'3'`、`'M'`、`'FR'`）。由于 `ChoicesMeta` 为所有成员生成 label（显式 label 或 `key.replace('_', ' ').title()` 自动生成），`self.label` 对所有正常成员都为 truthy。模拟真实错误："字符串化枚举应展示人类可读的 label"。利用了 `Choices` 类内置的 `label` 属性，代码自然，15 个 test_str 子测试均失败，但不抛异常。

---

### Group D — 替换

**原 mutation**：
```diff
diff --git a/django/db/models/enums.py b/django/db/models/enums.py
index ae20ef6d93..da181667cf 100644
--- a/django/db/models/enums.py
+++ b/django/db/models/enums.py
@@ -66,6 +66,8 @@ class Choices(enum.Enum, metaclass=ChoicesMeta):
         Use value when cast to str, so that Choices set as model instance
         attributes are rendered as expected in templates and similar contexts.
         """
+        if not getattr(self, '_str_initialized', False):
+            return repr(self)
         return str(self.value)
 
 
```
**分类**：🔴 必须替换

**理由**：`_str_initialized` 属性在任何枚举成员或 Choices 子类中均未被设置，`getattr` 永远返回 `False`，因此 `not False = True`，总是执行 `return repr(self)`，`return str(self.value)` 永远不可达。等价于返回 `repr()` 格式（`'<ClassName.MEMBER: value>'`）。属性名 `_str_initialized` 是无中生有的人工痕迹。

**最终 mutation**（替换）：
```diff
diff --git a/django/db/models/enums.py b/django/db/models/enums.py
index ae20ef6d93..e14c3944b6 100644
--- a/django/db/models/enums.py
+++ b/django/db/models/enums.py
@@ -66,7 +66,7 @@ class Choices(enum.Enum, metaclass=ChoicesMeta):
         Use value when cast to str, so that Choices set as model instance
         attributes are rendered as expected in templates and similar contexts.
         """
-        return str(self.value)
+        return repr(self.value) if isinstance(self.value, str) else str(self.value)
 
 
 class IntegerChoices(int, Choices):
```
**变异语义**：对字符串值（TextChoices）使用 `repr()` 而非 `str()`，结果带引号（如 `"'FR'"` 而非 `'FR'`、`"'GR'"` 而非 `'GR'`）；对整数值（IntegerChoices）行为正确（`repr(1) == str(1) == '1'`）。模拟真实错误：混淆 `repr()` 和 `str()`（Python 新手常见问题）。仅影响 TextChoices（8 个子测试失败），IntegerChoices 完全透明。

---

### Group E — 替换

**原 mutation**：
```diff
diff --git a/django/db/models/enums.py b/django/db/models/enums.py
index ae20ef6d93..be992b9de8 100644
--- a/django/db/models/enums.py
+++ b/django/db/models/enums.py
@@ -66,7 +66,9 @@ class Choices(enum.Enum, metaclass=ChoicesMeta):
         Use value when cast to str, so that Choices set as model instance
         attributes are rendered as expected in templates and similar contexts.
         """
-        return str(self.value)
+        if getattr(self.__class__, '_use_value_in_str', False):
+            return str(self.value)
+        return super().__str__()
 
 
 class IntegerChoices(int, Choices):
```
**分类**：🔴 必须替换

**理由**：`_use_value_in_str` 类属性在 `Choices` 及其所有子类（`IntegerChoices`、`TextChoices`、`Gender`、`Suit` 等）中均未定义，`getattr` 永远返回 `False`，始终执行 `return super().__str__()`。等价于完全还原 bug（`enum.Enum.__str__` 返回 `'ClassName.MEMBER'`）。与 Group D 同属虚假 feature flag 模式，人工痕迹明显。

**最终 mutation**（替换）：
```diff
diff --git a/django/db/models/enums.py b/django/db/models/enums.py
index ae20ef6d93..a285de8d55 100644
--- a/django/db/models/enums.py
+++ b/django/db/models/enums.py
@@ -66,7 +66,7 @@ class Choices(enum.Enum, metaclass=ChoicesMeta):
         Use value when cast to str, so that Choices set as model instance
         attributes are rendered as expected in templates and similar contexts.
         """
-        return str(self.value)
+        return str(self.value).lower()
 
 
 class IntegerChoices(int, Choices):
```
**变异语义**：对所有枚举成员的字符串值强制小写。对 `IntegerChoices`（`str('1').lower() == '1'`）无影响，但对 `TextChoices`（`'FR'.lower() == 'fr'`、`'M'.lower() == 'm'`）产生差异。模拟真实错误："URL-safe 或 case-insensitive 归一化"，开发者认为枚举值应标准化为小写。仅用整数选择的测试或只比较小写值的测试不会发现此问题（8 个 test_str 子测试失败）。

## 新设计 Mutation 说明

### Group B 新设计（`if isinstance(self.value, int): return str(self.name)`）

**代码分析基础**：`Choices.__str__` 被 `IntegerChoices` 和 `TextChoices` 共同继承，需要统一处理整数和字符串两种值类型。`ChoicesMeta` 为两种子类都设置了 `label` 和 `value` 属性。

**为什么选择此位置**：`isinstance(self.value, int)` 是在 `Choices.__str__` 中自然会出现的类型分支，模拟开发者对"不同类型的枚举应有不同字符串表示"的错误理解。IntegerChoices 成员的 value（如 `1`）和 name（如 `'DIAMOND'`）语义上都合理，容易混淆。

**模拟的真实错误**：开发者认为"整数枚举直接用名称字符串更有可读性和语义，字符串枚举的 value 本身就是想要的字符串"——前半部分错误，后半部分正确。仅 IntegerChoices 受影响，TextChoices 完全透明。

### Group C 新设计（`return str(self.label) if self.label else str(self.value)`）

**代码分析基础**：`ChoicesMeta.__new__` 为每个成员注入了 `label` 属性（`cls.label = property(lambda self: cls._value2label_map_.get(self.value))`），`label` 是人类可读标签（如 `'Jet Ski'`、`'Male'`），`value` 是机器存储值（如 `3`、`'M'`）。

**为什么选择此位置**：`label` 和 `value` 是 `Choices` 类内最重要的两个实例属性，容易混淆显示语义（应用于 UI）和序列化语义（应用于 API/DB）。由于 `label` 属性直接可访问，将其用于 `__str__` 是自然的"直觉错误"。`self.label` 对所有正常成员均为 truthy，所以 fallback 的 `str(self.value)` 分支实际永不执行。

**模拟的真实错误**：开发者认为 `__str__` 应返回"人类可读的展示名称"（label），而非"数据库存储的 key"（value），混淆了枚举值在 UI 层和数据层的不同角色。

### Group D 新设计（`repr(self.value) if isinstance(self.value, str) else str(self.value)`）

**代码分析基础**：Python 中 `repr()` 和 `str()` 的差异在字符串类型上最明显：`str('FR') == 'FR'` 而 `repr('FR') == "'FR'"`。整数类型则无差异（`repr(1) == str(1) == '1'`）。

**为什么选择此位置**：在 Python 中混淆 `repr()` 和 `str()` 是常见错误，尤其在处理"需要明确标识类型"的场景下。开发者可能为字符串值添加 `repr()` 以区分 `None`（空字符串和 None 的字符串表示不同），或者用于调试目的后忘记改回。

**模拟的真实错误**：开发者认为"字符串值应用 `repr()` 以便在日志/序列化中明确标识类型"，但破坏了 str(member) == str(member.value) 的契约。仅影响 TextChoices（引号问题），IntegerChoices 完全透明。

### Group E 新设计（`return str(self.value).lower()`）

**代码分析基础**：`TextChoices` 的值（如 `'FR'`、`'M'`、`'GR'`）通常是大写字母，`TextChoices._generate_next_value_` 返回 `name`（也是大写）。在 API 设计中，枚举值常用于 URL 路径参数或 JSON key，小写格式更为常见。

**为什么选择此位置**：`.lower()` 是字符串归一化的惯用写法，对整数转字符串（`'1'.lower() == '1'`）完全无害，仅对含有大写字母的字符串值有影响。这使得 mutation 只影响 TextChoices 子集，难以通过只测试 IntegerChoices 或只比较小写的测试发现。

**模拟的真实错误**：开发者为了 URL-safe 或 case-insensitive 场景，在 `__str__` 中添加了 `.lower()` 归一化，忽略了原始大写值是 Django TextChoices 的约定（如 `'FR'` 对应 `FRESHMAN`），破坏了值的原始大小写语义。
