# django__django-12858

## 问题背景

`Model._check_ordering()` 对 `Meta.ordering` 中使用 lookup（如 `supply__product__parent__isnull`）进行系统检查时，错误地抛出 `models.E015`，即使该排序实际上可以正常工作。

根本原因：在 `_check_ordering` 的关联字段解析循环中，当某个 `part`（如 `isnull`）不是字段名（抛出 `FieldDoesNotExist`）时，原代码只检查 `fld.get_transform(part) is None`——即只允许 transform 类型的后缀，不允许 lookup（如 `isnull`、`exact` 等）。Golden patch 将条件改为同时检查 transform 和 lookup，只有两者都为 None 时才报错。

## Golden Patch 语义分析

**修改前**（base-commit）：
```python
except (FieldDoesNotExist, AttributeError):
    if fld is None or fld.get_transform(part) is None:
        errors.append(...)
```
只检查 transform。`isnull` 是 lookup 而非 transform，`get_transform('isnull')` 返回 None，因此触发 E015。

**修改后**（golden patch）：
```python
except (FieldDoesNotExist, AttributeError):
    if fld is None or (
        fld.get_transform(part) is None and fld.get_lookup(part) is None
    ):
        errors.append(...)
```
同时检查 transform 和 lookup。对于 `isnull`：`get_transform` 返回 None，但 `get_lookup` 返回 `IsNull`（非 None），整体条件为 False → 不报错。对于真正不存在的字段/后缀：两者都返回 None → 仍然报错。

## 调用链分析

```
Model._check_ordering()                     # base.py ~L1715
  └── for field in related_fields:          # e.g., 'supply__product__parent__isnull'
      └── for part in field.split('__'):    # 逐 part 迭代
          └── _cls._meta.get_field(part)    # 正常字段解析
              → FieldDoesNotExist           # 当 part 是 lookup/transform 后缀
          └── fld.get_transform(part)       # RegisterLookupMixin.get_transform (query_utils.py)
          └── fld.get_lookup(part)          # RegisterLookupMixin.get_lookup (query_utils.py)

RegisterLookupMixin.get_lookup(lookup_name)   # query_utils.py L179
  └── self._get_lookup(lookup_name)           # 从 class_lookups 链查找
  └── if found and not issubclass(found, Lookup): return None   # 关键过滤
  └── return found

RegisterLookupMixin.get_transform(lookup_name) # query_utils.py L188
  └── self._get_lookup(lookup_name)
  └── if found and not issubclass(found, Transform): return None
  └── return found
```

`isnull` 注册为 `IsNull`（Lookup 子类，但非 Transform 子类），因此：
- `get_transform('isnull')` → None
- `get_lookup('isnull')` → `IsNull`（非 None）

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | `fld.get_lookup(part) is None` → `fld.get_transform(part) is None`，使检查等价于 `transform is None and transform is None`，与 base-commit 行为相同 |
| B | 🟡 语义浅层 | 保留 | `and` → `or`，虽是单运算符替换，但修改在核心条件连接处，语义显著改变 |
| C | 🔴 必须替换 | 替换 | `fld.get_transform(part) is None`（只保留 transform 检查），等价于 base-commit，无功能差别 |
| D | 🟢 保留 | 保留 | 条件加了 `if _cls is not None else True`，在非关联字段场景下强制 lookup 检查为 None，修改微妙 |
| E | 🔴 必须替换 | 替换 | 精确等于 base-commit 代码（去掉 `and fld.get_lookup(part) is None`），直接还原 |

语义浅层共 1 个（B），floor(1/2)=0，无需替换。
必须替换 3 个（A、C、E）。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
-                        fld.get_transform(part) is None and fld.get_lookup(part) is None
+                        fld.get_transform(part) is None and fld.get_transform(part) is None
```
**分类**：🔴 必须替换
**理由**：将 `get_lookup` 替换为 `get_transform`，使条件变为 `transform is None and transform is None`，即 `transform is None`（冗余 and）。功能上等价于 base-commit 的 `fld.get_transform(part) is None`，只是多了一次重复调用。Fix 完全无效，lookup 仍被错误标记为 E015。
**最终 mutation**（替换，跨文件变异到 `query_utils.py`）：
```diff
diff --git a/django/db/models/query_utils.py b/django/db/models/query_utils.py
index 4c6c48bf9d..30ac184b99 100644
--- a/django/db/models/query_utils.py
+++ b/django/db/models/query_utils.py
@@ -181,7 +181,7 @@ class RegisterLookupMixin:
         found = self._get_lookup(lookup_name)
         if found is None and hasattr(self, 'output_field'):
             return self.output_field.get_lookup(lookup_name)
-        if found is not None and not issubclass(found, Lookup):
+        if found is not None and not issubclass(found, Transform):
             return None
         return found
```
**变异语义**：`RegisterLookupMixin.get_lookup` 中，将判断 `found` 是否为 `Lookup` 子类改为判断是否为 `Transform` 子类。这使得 `get_lookup` 只对真正的 Transform 类型返回非 None（与 `get_transform` 行为相同）。对 `isnull`（`IsNull` 是 Lookup 但非 Transform）：`get_lookup('isnull')` 现在返回 None，使 `_check_ordering` 的 `and` 条件为 `True and True = True`，E015 仍被报告。跨文件变异（`query_utils.py`），代码审查仅看 `base.py` 时完全看不出问题，隐蔽性极强。

---

### Group B — 保留
**原 mutation**：
```diff
-                        fld.get_transform(part) is None and fld.get_lookup(part) is None
+                        fld.get_transform(part) is None or fld.get_lookup(part) is None
```
**分类**：🟡 语义浅层（保留）
**理由**：`and` → `or` 是单运算符替换，但修改在核心错误条件处。语义变为"transform 为 None 或 lookup 为 None"，即只需一个不存在就报错。对 `isnull`：transform 为 None，lookup 非 None；整体 `True or False = True` → 仍报错。对注册为 transform 的后缀（如 `Lower`）：transform 非 None，lookup 为 None（非 Lookup 子类）；整体 `False or True = True` → 新增误报。此 mutation 在关键逻辑节点，能模拟开发者混淆 `and`/`or` 的真实错误，且影响 transform 场景，与其他 mutation 不完全重叠。
**最终 mutation**（与原相同）：
```diff
diff --git a/django/db/models/base.py b/django/db/models/base.py
index bc6f7d283e..b0115f94e6 100644
--- a/django/db/models/base.py
+++ b/django/db/models/base.py
@@ -1748,7 +1748,7 @@ class Model(metaclass=ModelBase):
                         _cls = None
                 except (FieldDoesNotExist, AttributeError):
                     if fld is None or (
-                        fld.get_transform(part) is None and fld.get_lookup(part) is None
+                        fld.get_transform(part) is None or fld.get_lookup(part) is None
                     ):
                         errors.append(
```
**变异语义**：条件由"既不是 transform 也不是 lookup → 报错"改为"不是 transform 或不是 lookup → 报错"。对 lookup 类型（如 `isnull`）：`transform is None OR lookup is not None` → `True OR False = True` → 仍报 E015。对已有 transform 类型测试（如 `Lower`）：`False OR True = True` → 多出误报。Passes 基础测试（没有 lookup 的 ordering），fails lookup ordering 测试。

---

### Group C — 替换
**原 mutation**：
```diff
-                        fld.get_transform(part) is None and fld.get_lookup(part) is None
+                        fld.get_transform(part) is None
```
**分类**：🔴 必须替换
**理由**：`fld.get_transform(part) is None and fld.get_lookup(part) is None` → `fld.get_transform(part) is None`，精确等价于 base-commit 原始代码（去掉了 `and fld.get_lookup(part) is None`），只是写法略不同（原始代码没有括号）。Fix 完全失效。
**最终 mutation**（替换，改用 `field` 代替 `part` 变量名）：
```diff
diff --git a/django/db/models/base.py b/django/db/models/base.py
index bc6f7d283e..329ec20dcd 100644
--- a/django/db/models/base.py
+++ b/django/db/models/base.py
@@ -1748,7 +1748,7 @@ class Model(metaclass=ModelBase):
                         _cls = None
                 except (FieldDoesNotExist, AttributeError):
                     if fld is None or (
-                        fld.get_transform(part) is None and fld.get_lookup(part) is None
+                        fld.get_transform(part) is None and fld.get_lookup(field) is None
                     ):
                         errors.append(
```
**变异语义**：`get_lookup(part)` → `get_lookup(field)`。内层循环变量 `part` 是当前解析到的单段字符串（如 `isnull`），而 `field` 是完整的 ordering 字段字符串（如 `supply__product__parent__isnull`）。`fld.get_lookup('supply__product__parent__isnull')` 不匹配任何已注册的 lookup（lookup 注册用短名 `isnull`，不用完整路径），所以 `get_lookup(field)` 始终返回 None，整体条件退化为 `transform is None and True = transform is None`——等同于只检查 transform。对简单情形 `test__isnull`（field='test__isnull'），`get_lookup('test__isnull')` 也返回 None，fix 同样失效。模拟了开发者混淆 `part`（当前段）与 `field`（完整字段路径）的真实错误。

---

### Group D — 保留
**原 mutation**：
```diff
-                        fld.get_transform(part) is None and fld.get_lookup(part) is None
+                        fld.get_transform(part) is None and (fld.get_lookup(part) is None if _cls is not None else True)
```
**分类**：🟢 保留
**理由**：在 lookup 检查前加了 `if _cls is not None else True` 的条件。`_cls` 在遇到非关联字段后被设为 None。当 `_cls is None` 时，lookup 检查强制为 True（总认为 lookup 不存在），等效于只检查 transform。当 `_cls is not None` 时（仍在关联字段链中），行为与 fix 相同。这模拟了开发者"关联字段链末端不需要检查 lookup"的错误假设。在 `test__isnull` 这类简单场景（`_cls = None` after `test` field）中，fix 失效；在更复杂的关联链场景中行为可能正确，增加了检测难度。修改位置独特，与其他 mutation 不重叠。
**最终 mutation**（与原相同）：
```diff
diff --git a/django/db/models/base.py b/django/db/models/base.py
index bc6f7d283e..9cc7ef21b3 100644
--- a/django/db/models/base.py
+++ b/django/db/models/base.py
@@ -1748,7 +1748,7 @@ class Model(metaclass=ModelBase):
                         _cls = None
                 except (FieldDoesNotExist, AttributeError):
                     if fld is None or (
-                        fld.get_transform(part) is None and fld.get_lookup(part) is None
+                        fld.get_transform(part) is None and (fld.get_lookup(part) is None if _cls is not None else True)
                     ):
                         errors.append(
```
**变异语义**：`_cls` 在处理完 CharField/IntegerField 等非关联字段后被设为 None（代码 `else: _cls = None`）。在下一个迭代中（lookup 部分），`_cls` 为 None，条件变为 `transform is None and True`，即只检查 transform。`isnull` 非 transform → 仍报 E015。而对于深度关联链如 `fk__fk__isnull`，前两段使 `_cls` 更新，到 `isnull` 时 `_cls` 是 `fk` 指向的模型（非 None），条件正常检查 lookup → 不报错。此 mutation 仅在字段+lookup 的两段模式下失败，在更长关联链末尾 lookup 时表现正确，检测难度较高。

---

### Group E — 替换
**原 mutation**：
```diff
-                    if fld is None or (
-                        fld.get_transform(part) is None and fld.get_lookup(part) is None
-                    ):
+                    if fld is None or fld.get_transform(part) is None:
```
**分类**：🔴 必须替换
**理由**：恢复成 base-commit 原始代码，精确等于 patch 前的状态，是 golden patch 的直接逆操作。
**最终 mutation**（替换，反转 lookup 检查方向）：
```diff
diff --git a/django/db/models/base.py b/django/db/models/base.py
index bc6f7d283e..01d9a1a7b0 100644
--- a/django/db/models/base.py
+++ b/django/db/models/base.py
@@ -1748,7 +1748,7 @@ class Model(metaclass=ModelBase):
                         _cls = None
                 except (FieldDoesNotExist, AttributeError):
                     if fld is None or (
-                        fld.get_transform(part) is None and fld.get_lookup(part) is None
+                        fld.get_transform(part) is None and fld.get_lookup(part) is not None
                     ):
                         errors.append(
```
**变异语义**：`lookup is None` → `lookup is not None`，条件变为"transform 为 None 且 lookup 非 None → 报错"。对 `isnull`（非 transform，是 lookup）：`True and True = True` → 报 E015。对真正不存在的后缀（非 transform 非 lookup）：`True and False = False` → **不报**错。即：合法的 lookup 被拒绝，不合法的后缀被放行——两个场景的错误方向完全对调。F2P 测试（lookup ordering 不应报错）失败；同时使非法 ordering 通过验证，影响到既有的 P2P 测试。行为完全对称翻转，代码读起来语法完全正确，需要深入理解 lookup 语义才能发现。

## 新设计 Mutation 说明

### Group A 新设计依据
Golden patch 的 `fld.get_lookup(part)` 依赖于 `RegisterLookupMixin.get_lookup` 中的 `issubclass(found, Lookup)` 过滤器。将过滤器改为 `issubclass(found, Transform)` 使 `get_lookup` 只认 Transform，与 `get_transform` 行为完全相同。从 `base.py` 看，fix 代码完全正确（`transform is None and lookup is None`），但因为 `get_lookup` 在 `query_utils.py` 中行为被改变，效果与只检查 `transform` 相同。跨文件变异是最难被单独代码审查发现的类型。

### Group C 新设计依据
内层循环使用两个变量：`field`（完整路径如 `parent__isnull`）和 `part`（当前段如 `isnull`）。Lookup 注册时用短名（`isnull`），而不是路径字符串（`parent__isnull`），所以 `get_lookup(field)` 总返回 None。这个错误模拟了开发者在嵌套循环中混淆变量作用域（`field` 是外层，`part` 是内层）的典型错误。从代码审查角度，`fld.get_lookup(field)` 和 `fld.get_lookup(part)` 在语法上完全相似，只有理解变量的 scope 才能发现问题。

### Group E 新设计依据
将 `lookup is None` 改为 `lookup is not None` 完全反转 lookup 的角色——有效的 lookup 被报错，无效的后缀被放行。这两种行为都是错误的，且错误方向完全相反，模拟了开发者在写"发现 lookup 时跳过报错"时误写成"发现 lookup 时触发报错"的逻辑颠倒。代码读起来非常自然（`transform is None and lookup is not None`），只有深入思考语义才能意识到方向错了。
