# django__django-13590

## 问题背景

从 Django 2.2 升级到 3.0 后，使用命名元组（namedtuple）作为 `__range` lookup 参数的 QuerySet 过滤器会报错：`TypeError: __new__() missing 1 required positional argument: 'far'`。

根本原因：`Query.resolve_lookup_value()` 处理 list/tuple 时，会遍历各元素进行递归解析，然后重建相同类型的容器。Django 3.0 之前的代码直接用 `type(value)(generator)` 重建，对普通 list/tuple 有效（因为它们接受可迭代对象作为构造参数）。但 namedtuple 的构造函数要求每个字段作为独立位置参数，传入 generator 作为单一参数会报缺少参数的 TypeError。

## Golden Patch 语义分析

```python
# 原代码（Django 3.0 之前）
return type(value)(
    self.resolve_lookup_value(sub_value, can_reuse, allow_joins)
    for sub_value in value
)

# 修复后
values = (
    self.resolve_lookup_value(sub_value, can_reuse, allow_joins)
    for sub_value in value
)
type_ = type(value)
if hasattr(type_, '_make'):  # namedtuple
    return type_(*values)
return type_(values)
```

关键设计决策：
1. **`hasattr(type_, '_make')`**：`_make` 是所有 namedtuple 的标准类方法，用于从可迭代对象创建实例。这是检测 namedtuple 的规范方式（而非 `isinstance` 或检查 `_fields`）。
2. **`type_(*values)`**：对 namedtuple 展开 generator，将每个解析后的值作为独立位置参数传入——这是 namedtuple 构造函数所要求的。
3. **`type_(values)`**：对普通 list/tuple 直接传入 generator，保留原有行为。

## 调用链分析

```
QuerySet.filter(num_employees__range=EmployeeRange(51, 100))
    └─> Query.build_filter()
            └─> Query.resolve_lookup_value(value=EmployeeRange(51, 100), ...)
                    ├─ isinstance(value, (list, tuple)) → True (namedtuple IS a tuple)
                    ├─ 遍历 value → 对每个元素递归调用 resolve_lookup_value
                    ├─ type_ = type(value) = EmployeeRange
                    ├─ hasattr(EmployeeRange, '_make') → True
                    └─ return EmployeeRange(*values)  ← 正确：各字段作为独立参数

对比 type_(values) 的失败路径：
    EmployeeRange(generator) → __new__() 只收到1个参数（generator对象）
    而 EmployeeRange.__new__ 需要 minimum 和 maximum 两个参数 → TypeError
```

## 替换决策总览

| 组 | 原有 Mutation | 分类 | 决策 | 原因摘要 |
|---|---|---|---|---|
| A | `type_(*values)` → `type_(values)` | 🔴 必须替换 | 替换 | 与 B/D/E 完全相同 diff，4个组完全重复 |
| B | `type_(*values)` → `type_(values)` | 🔴 必须替换 | 替换 | 同 A，重复 |
| C | 不存在 | — | 新设计 | mutations.jsonl 中无 C 组，需补全 |
| D | `type_(*values)` → `type_(values)` | 🔴 必须替换 | 替换 | 同 A，重复 |
| E | `type_(*values)` → `type_(values)` | 🔴 必须替换 | 替换 | 同 A，重复 |

所有原有4个 mutation 都是 **完全相同的 diff**（仅改 `type_(*values)` 为 `type_(values)`），属于功能等价重复，必须全部替换。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**（与 B/D/E 完全相同，必须替换）

**替换后 mutation**（反转 hasattr 条件判断）：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index ee98984826..140c79497b 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1082,7 +1082,7 @@ class Query(BaseExpression):
                 for sub_value in value
             )
             type_ = type(value)
-            if hasattr(type_, '_make'):  # namedtuple
+            if not hasattr(type_, '_make'):  # not a namedtuple
                 return type_(*values)
             return type_(values)
         return value
```
**变异语义**：将 namedtuple 检测条件取反。非 namedtuple（list/tuple）走 `type_(*values)` 路径（可能错误），namedtuple 走 `type_(values)` 路径 → TypeError。对于 `EmployeeRange.__new__` 需要 minimum 和 maximum 两个参数，传入 generator 只给了1个参数 → `TypeError` → `test_range_lookup_namedtuple` 失败。这模拟了开发者对 `hasattr` 条件含义的误解（"_make 表示它支持迭代器" → "没有 _make 才需要展开"）。

---

### Group B — 替换
**替换后 mutation**（使用 `isinstance(value, list)` 代替 `hasattr` 检测）：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index ee98984826..92ab5b7b50 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1082,7 +1082,7 @@ class Query(BaseExpression):
                 for sub_value in value
             )
             type_ = type(value)
-            if hasattr(type_, '_make'):  # namedtuple
+            if isinstance(value, list):  # list type
                 return type_(*values)
             return type_(values)
         return value
```
**变异语义**：检测 list 来决定是否展开，而不是检测 namedtuple。namedtuple 是 tuple 的子类，不是 list → `isinstance(namedtuple_instance, list)` 为 False → 走 `type_(values)` 路径 → TypeError。这模拟了开发者将 list 和 tuple 的构造语义混淆（"list 才需要展开 generator"），忽略了 namedtuple 的特殊性。

---

### Group C — 新设计（补全）
**分类**：新设计（C3 类型：使用不存在的属性进行检测）
**设计思路**：将 `hasattr(type_, '_make')` 改为 `hasattr(type_, '_create')`。`_create` 是 namedtuple 不具备的属性，检测结果始终为 False → 永远走 `type_(values)` 路径 → TypeError for namedtuple。`_create` 看起来与 `_make` 语义相似（都像是"创建"），是开发者在拼写或查阅 namedtuple API 时可能犯的错误。
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index ee98984826..904b9dbb7f 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1082,7 +1082,7 @@ class Query(BaseExpression):
                 for sub_value in value
             )
             type_ = type(value)
-            if hasattr(type_, '_make'):  # namedtuple
+            if hasattr(type_, '_create'):  # namedtuple
                 return type_(*values)
             return type_(values)
         return value
```
**变异语义**：`_create` 不是 namedtuple 的标准属性，`hasattr(EmployeeRange, '_create')` 始终为 False → 条件永不成立 → namedtuple 永远走 `type_(values)` → TypeError。F2P 测试的 namedtuple 过滤器报错。

---

### Group D — 替换
**替换后 mutation**（使用 `type_ is tuple` 精确类型检查代替 `hasattr`）：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index ee98984826..cc67ff3416 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1082,7 +1082,7 @@ class Query(BaseExpression):
                 for sub_value in value
             )
             type_ = type(value)
-            if hasattr(type_, '_make'):  # namedtuple
+            if type_ is tuple:  # plain tuple
                 return type_(*values)
             return type_(values)
         return value
```
**变异语义**：使用 `type_ is tuple` 检查是否为 **普通 tuple 类型**（不含子类），而 namedtuple 是 tuple 的子类，`type(EmployeeRange(51, 100)) is EmployeeRange`，非 `tuple` → 条件为 False → 走 `type_(values)` → TypeError。这模拟了开发者使用 `is` 而非 `issubclass` 或 `hasattr` 进行类型检查，无法识别 tuple 的子类。

---

### Group E — 替换
**替换后 mutation**（将解析后的值逆序传入 namedtuple）：
```diff
diff --git a/django/db/models/sql/query.py b/django/db/models/sql/query.py
index ee98984826..dc9f7c8647 100644
--- a/django/db/models/sql/query.py
+++ b/django/db/models/sql/query.py
@@ -1077,13 +1077,13 @@ class Query(BaseExpression):
         elif isinstance(value, (list, tuple)):
             # The items of the iterable may be expressions and therefore need
             # to be resolved independently.
-            values = (
+            values = [
                 self.resolve_lookup_value(sub_value, can_reuse, allow_joins)
                 for sub_value in value
-            )
+            ]
             type_ = type(value)
             if hasattr(type_, '_make'):  # namedtuple
-                return type_(*values)
+                return type_(*reversed(values))
             return type_(values)
         return value
```
**变异语义**：将 namedtuple 的字段值逆序传入构造函数。对 `EmployeeRange(minimum=51, maximum=100)`，解析后 values=[51, 100]，逆序后得 `EmployeeRange(minimum=100, maximum=51)`。SQL range lookup 变为 `num_employees BETWEEN 100 AND 51`，这是一个矛盾范围（在大多数数据库上返回空集），`assertSequenceEqual(qs, [self.c5])` 失败（期望1条记录，得到0条）。这是一种**数据而非异常**类的变异，更难被发现：代码不报错，但返回错误结果。

## 新设计 Mutation 说明

原有4个 mutation（A/B/D/E）全为相同 diff，仅以下维度不同：各组均将 `type_(*values)` → `type_(values)`，使 namedtuple 收到 generator 对象而非展开参数，触发 TypeError。这4个等价 mutation 违背了 mutation 多样性原则，均需替换。

新设计保留了相同的"破坏 namedtuple 路径"语义，但通过5种不同机制实现：
- **A**：逻辑取反（`not hasattr`）
- **B**：类型检查方向错误（`isinstance(value, list)`）
- **C**：拼写相近的错误属性（`_create` vs `_make`）
- **D**：精确类型检查误杀子类（`type_ is tuple`）
- **E**：逆序传参（最隐蔽，不报错但返回错误结果）

特别地，mutation E 是唯一不触发 TypeError 的变异，而是产生错误的查询结果，是最难通过代码审查发现的。
