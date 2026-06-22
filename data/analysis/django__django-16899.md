# django__django-16899

## 问题背景

`ModelAdmin` 对 `readonly_fields` 的校验错误信息不含字段名。原信息形如 `The value of 'readonly_fields[0]' is not a callable, an attribute of 'CityInline', or an attribute of 'admin_checks.City'.`——只给出索引位置 `[0]`，不含出错字段的名字。其它字段（list_editable 等）的错误信息都会带上具体值。Golden patch 统一格式：在信息中加入 `refers to '<field_name>'`，变成 `The value of 'readonly_fields[1]' refers to 'nonexistent', which is not a callable, ...`，便于定位。

## Golden Patch 语义分析

```python
checks.Error(
    "The value of '%s' refers to '%s', which is not a callable, "
    "an attribute of '%s', or an attribute of '%s'."
    % (
        label,
        field_name,
        obj.__class__.__name__,
        obj.model._meta.label,
    ),
    obj=obj.__class__,
    id="admin.E035",
)
```
核心语义：**错误信息格式串新增一个 `refers to '%s'` 占位，并在格式化参数元组中相应加入 `field_name`（位于 `label` 之后、`obj.__class__.__name__` 之前）**。格式串占位数（4 个 `%s`）必须与参数个数（label, field_name, 类名, model label）严格匹配，且 `field_name` 的位置（第二个）决定它出现在 "refers to '...'" 处。占位与参数的数量、顺序都是修复要点。

F2P 测试 `SystemChecksTestCase.test_nonexistent_field` 与 `test_nonexistent_field_on_inline`：断言 E035 错误信息为带 `refers to 'nonexistent'` / `refers to 'i_dont_exist'` 的新格式文本。

## 调用链分析

`_check_readonly_fields` 用 `enumerate(obj.readonly_fields)` 生成 `label="readonly_fields[%d]" % index`，对每个 field_name 调 `_check_readonly_fields_item(obj, field_name, label)`。后者依次检查 callable / hasattr(obj) / hasattr(obj.model) / `get_field`，全失败则返回带新格式的 `checks.Error`。label 索引、callable 守卫、格式串占位/参数个数与顺序、信息文本本身，任一出错都会让 F2P 断言文本不符或抛异常。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | label 索引 `index`→`index-1 if index>0`，标签索引错位 |
| B | 🟢 高质量 | 保留 | `if callable(field_name)`→`if not callable(...)`，校验守卫反转 |
| C | 🟢 高质量 | 保留 | 删参数元组里的 `field_name`，4 占位仅 3 参数 → TypeError |
| D | ➕ 补充 | 新增 | `obj.__class__.__name__` 与 `field_name` 两实参顺序对调 |
| E | ➕ 补充 | 新增 | 详细信息藏到 `verbose_readonly_errors` 开关后（默认走旧文本） |

原始仅 A/B/C/D 四组，且 D 与 C 都改格式化参数元组（C 删 field_name、D 删 field_name 行——D 实际与 C 高度趋同）。保留 A/B/C，重做 D 为"实参顺序对调"，补充 E（默认关闭开关）。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：label 索引错位）
```diff
                     self._check_readonly_fields_item(
-                        obj, field_name, "readonly_fields[%d]" % index
+                        obj, field_name, "readonly_fields[%d]" % (index - 1 if index > 0 else index)
                     )
```
**变异语义**：label 里的索引由 `index` 改成 `index-1 if index>0 else index`。错误信息中的位置标签错位——`readonly_fields[1]` 变成 `readonly_fields[0]`。F2P 断言完整文本（含 `readonly_fields[1]`/`[0]`），位置不符 → 失败。模拟"索引计算 off-by-one"。保留。

### Group B — 保留（B3 条件反转：callable 守卫）
```diff
     def _check_readonly_fields_item(self, obj, field_name, label):
-        if callable(field_name):
+        if not callable(field_name):
             return []
```
**变异语义**：`if callable(field_name): return []` 反转成 `if not callable(...)`。普通字符串字段名（非 callable）原本要继续校验，现直接 `return []` 不报错；而 callable 反而走后续校验。不存在的字段名（字符串）不再产生 E035 错误 → F2P 期望有错误却得到空 → 失败。保留。

### Group C — 保留（C1 类型：删参数致占位失配）
```diff
                         "an attribute of '%s', or an attribute of '%s'."
                         % (
                             label,
-                            field_name,
                             obj.__class__.__name__,
                             obj.model._meta.label,
                         ),
```
**变异语义**：从格式化参数元组删除 `field_name`，但格式串仍是 golden 的 4 占位（`refers to '%s'` 那个还在）。4 个 `%s` 只剩 3 个参数 → `TypeError: not enough arguments for format string`，check 直接抛异常。F2P 失败（异常而非预期错误对象）。模拟"改格式时删了参数、漏改占位数"。保留。

### Group D — 补充（D4 状态：实参顺序对调）
```diff
                         % (
                             label,
-                            field_name,
                             obj.__class__.__name__,
+                            field_name,
                             obj.model._meta.label,
                         ),
```
**变异语义**：把 `field_name` 与 `obj.__class__.__name__` 两个格式化实参的顺序对调（field_name 移到类名之后）。占位数仍是 4、不报 TypeError，但 `refers to '%s'` 处填的是类名、第三个 `%s` 处填的是 field_name——错误信息里字段名和类名位置互换。F2P 断言精确文本（`refers to 'nonexistent'` + `attribute of 'SongAdmin'`），位置错 → 失败。模拟"调整格式化参数时顺序写反"。比 C（删参数 TypeError）隐蔽——不崩溃、只是文本错位。补充为 D。

### Group E — 补充（E2 隐式→显式开关）
```diff
                 return [
                     checks.Error(
-                        "The value of '%s' refers to '%s', which is not a callable, "
-                        ... % (label, field_name, obj.__class__.__name__, obj.model._meta.label),
+                        (
+                            "The value of '%s' refers to '%s', which is not a callable, "
+                            ... % (label, field_name, obj.__class__.__name__, obj.model._meta.label)
+                        )
+                        if getattr(self, "verbose_readonly_errors", False)
+                        else (
+                            "The value of '%s' is not a callable, an attribute of "
+                            "'%s', or an attribute of '%s'."
+                            % (label, obj.__class__.__name__, obj.model._meta.label)
+                        ),
                         obj=obj.__class__,
                         id="admin.E035",
                     )
                 ]
```
**变异语义**：把 golden 的详细信息（含 `refers to '<field>'`）藏到 `self.verbose_readonly_errors` 开关后（默认 False），默认走 `else` 分支的旧文本（不含字段名）。默认构造的 ModelAdminChecks 不设该属性 → 还原原始错误文本。F2P 断言新文本却得到旧文本 → 失败。模拟"把详细错误信息做成可配置、默认却关掉"。补充为 E。

## 新设计 Mutation 说明

原始仅 A/B/C/D 四组，且 D 与 C 都作用于格式化参数元组（删 field_name），机制趋同。本次保留 A（label 索引错位）、B（callable 守卫反转）、C（删参数致 TypeError），把与 C 趋同的 D 重做为"实参顺序对调"（不崩溃但文本错位），补充 E（`verbose_readonly_errors` 默认关闭开关，退回旧文本）。五组覆盖"索引错位 / 守卫反转 / 占位失配崩溃 / 实参顺序对调 / 默认关闭开关"五个角度，分别作用于 label 生成、callable 守卫、参数个数、参数顺序、特性开关五个环节。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
