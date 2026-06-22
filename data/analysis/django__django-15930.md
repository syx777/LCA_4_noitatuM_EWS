# django__django-15930

## 问题背景

`Case(When(~Q(pk__in=[]), then=...))` 崩溃。`~Q(pk__in=[])` 是"匹配所有行"的谓词，在 WHERE 子句里被编译成**空字符串**（无需过滤）。但在 `CASE WHEN <condition> THEN ...` 里，空 condition 生成非法 SQL `CASE WHEN THEN ...`（`WHEN` 后直接 `THEN`），报 `syntax error at or near "THEN"`。Golden patch 在 `When.as_sql` 中：当 `condition_sql == ""` 时，把它替换成编译 `Value(True)` 得到的恒真谓词，使 `CASE WHEN True THEN ...` 合法且语义正确（匹配所有行）。

## Golden Patch 语义分析

```python
condition_sql, condition_params = compiler.compile(self.condition)
# Filters that match everything are handled as empty strings in the
# WHERE clause, but in a CASE WHEN expression they must use a predicate
# that's always True.
if condition_sql == "":
    condition_sql, condition_params = compiler.compile(Value(True))
template_params["condition"] = condition_sql
```
核心语义：**WHERE 子句允许"匹配全部"用空串表示，但 CASE WHEN 的 condition 不能为空——必须显式给一个恒真谓词 `Value(True)`**。判据是 `condition_sql == ""`（编译后为空），替换值是 `compiler.compile(Value(True))`（生成 `True`/`1` 之类恒真 SQL 及其参数）。注意要同时替换 `condition_sql` 和 `condition_params`，因为新谓词可能带参数。

F2P 测试 `CaseExpressionTests.test_annotate_with_full_when`：`Case(When(~Q(pk__in=[]), then=Value("selected")), default=Value("not selected"))`，断言所有行 `selected == "selected"`（全部命中恒真 When）。

## 调用链分析

`Case.as_sql` 逐个编译 `When` 子句 → `When.as_sql` 调 `compiler.compile(self.condition)` 得到 `condition_sql`。`~Q(pk__in=[])` 经 WhereNode 编译为 `("", [])`（空 SQL）。模板 `"WHEN %(condition)s THEN %(result)s"` 用空 condition 拼出 `WHEN  THEN ...` 非法。修复在 `condition_sql == ""` 时替换为 `Value(True)` 的编译结果。`condition_params` 必须同步更新，否则参数占位与值错位。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 补充 | 新增 | 原缺 A 组；替换值用 `Value(False)`，恒假谓词使全不命中 |
| B | 🟢 高质量 | 保留 | 注释掉守卫，空 condition 直接进模板，还原 bug |
| C | 🟢 高质量 | 保留 | `== ""`→`is None`，空串不等于 None，守卫不触发 |
| D | 🟢 高质量 | 保留 | 删除整个守卫 |
| E | 🔴 必须替换 | 替换 | 原 E 与 C 字节完全相同（`is None`）；改为默认关闭开关 |

原 C、E 两组字节完全相同（都把 `== ""` 改成 `is None`）。保留 C，把 E 重做为开关 gate，补充 A。

## 各组 Mutation 分析

### Group A — 补充（A1 接口契约：Value(False) 恒假）
```diff
         if condition_sql == "":
-            condition_sql, condition_params = compiler.compile(Value(True))
+            condition_sql, condition_params = compiler.compile(Value(False))
```
**变异语义**：空 condition 替换成 `Value(False)`（恒假）而非 `Value(True)`。SQL 合法（`CASE WHEN False THEN ...`），不再语法报错，但语义完全相反——"匹配全部"的 When 变成"匹配无"，所有行落到 `default`。F2P 断言所有行 `== "selected"`，实际全是 `"not selected"`，断言失败。比"删守卫导致语法错误"更隐蔽：SQL 能跑、只是结果错。模拟"True/False 谓词搞反"。

### Group B — 保留（D-注释守卫）
```diff
-        if condition_sql == "":
-            condition_sql, condition_params = compiler.compile(Value(True))
+        # if condition_sql == "":
+        #     condition_sql, condition_params = compiler.compile(Value(True))
```
**变异语义**：注释掉守卫，空 condition 原样进模板，生成 `WHEN  THEN ...` 语法错误，还原原 bug。保留。

### Group C — 保留（C1 类型/数据形状：is None）
```diff
-        if condition_sql == "":
+        if condition_sql is None:
```
**变异语义**：判据从"空字符串"改成"是 None"。`compiler.compile` 对全匹配谓词返回的是空**字符串** `""` 而非 `None`，故 `is None` 永为 False → 守卫不触发 → 空 condition 进模板 → 语法错误。模拟"用 is None 判空、漏了空字符串"的经典边界错误。保留。

### Group D — 保留（B2 删除守卫）
```diff
-        if condition_sql == "":
-            condition_sql, condition_params = compiler.compile(Value(True))
         template_params["condition"] = condition_sql
```
**变异语义**：彻底删除守卫，与注释（B）效果相同但形式是直接删行。空 condition 进模板报错。保留。

### Group E — 替换（E2 隐式→显式开关）
**原**：与 C 字节完全相同（`is None`）。
**最终 mutation**：
```diff
-        if condition_sql == "":
+        if condition_sql == "" and getattr(self, "fix_empty_condition", False):
             condition_sql, condition_params = compiler.compile(Value(True))
```
**变异语义**：在守卫条件后追加实例属性开关 `fix_empty_condition`，默认 `False`。判据 `condition_sql == ""` 即便为真，因 `and False` 整体为假 → 不替换 → 空 condition 进模板报错。只有显式设 `fix_empty_condition=True` 才修复。模拟"把修复做成可配置、默认却关掉"。与 C（`is None` 边界误判）机制不同。

## 新设计 Mutation 说明

原 C、E 两组字节完全相同（都把 `== ""` 改成 `is None`），缺 A。本次保留 B（注释守卫）、C（`is None` 边界误判）、D（删守卫），补充 A（替换值 `Value(False)` 使语义反转但 SQL 合法），把与 C 重复的 E 重做为默认关闭的 `fix_empty_condition` 开关。五组覆盖"恒假谓词 / 注释守卫 / is-None 边界 / 删守卫 / 默认关闭开关"五个角度。其中 A 尤为隐蔽——它不引发语法错误，只让结果语义相反。全部实测：golden 通过、五个变异均令 F2P（`test_annotate_with_full_when`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
