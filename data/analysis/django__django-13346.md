# django__django-13346

## 问题背景

在 MySQL、Oracle 和 SQLite 数据库上，对 `JSONField` 的 key transform 使用 `__in` 查询时，结果为空（0 条记录），而直接使用 `=` 过滤可以正常返回数据。根本原因是 Django 的 `KeyTransform` 没有注册 `In` lookup 的数据库特定处理方式：非 PostgreSQL 数据库（无原生 JSON 字段支持）在做 `IN` 查询时，RHS 的占位符 `%s` 没有被替换成数据库的 JSON 提取表达式（如 `JSON_EXTRACT(%s, '$')`），导致查询比较失败。

Golden patch 新增了 `KeyTransformIn` 类（继承 `lookups.In`），重写了 `process_rhs` 方法，为不同数据库供应商（Oracle、MariaDB、SQLite/MySQL）分别包裹正确的 JSON 提取函数，并在最后将 `KeyTransformIn` 注册到 `KeyTransform`。

## Golden Patch 语义分析

修复的核心逻辑：`process_rhs` 接管了 `IN` lookup 的 RHS 处理，在 `has_native_json_field=False` 时，针对不同数据库供应商将绑定参数占位符 `%s` 替换为 JSON 提取表达式。

- **Oracle**：使用 `JSON_QUERY`（列表/字典）或 `JSON_VALUE`（标量），值直接内联进 SQL，`rhs_params` 清空。
- **MySQL MariaDB**：使用 `JSON_UNQUOTE(JSON_EXTRACT(%s, '$'))` 去除 JSON 字符串引号。
- **SQLite/MySQL（非 MariaDB）**：使用 `JSON_EXTRACT(%s, '$')` 提取根值。

关键操作：`rhs = rhs % func` 将 format string 的 `%s` 占位符替换为上述表达式。最后通过 `KeyTransform.register_lookup(KeyTransformIn)` 把该 lookup 注册到 key transform 上。

## 调用链分析

```
NullableJSONModel.objects.filter(value__foo__in=[...])
  → KeyTransform.__init__ (key_name='foo')
  → KeyTransform.get_lookup('in')  ← 通过 register_lookup 找到 KeyTransformIn
  → KeyTransformIn.process_rhs(compiler, connection)
    → super().process_rhs() (lookups.In)  ← 返回标准 %s 占位符
    → 根据 connection.vendor 替换为 JSON 提取表达式
  → SQL 生成: JSON_EXTRACT(field, '$.foo') IN (JSON_EXTRACT(%s, '$'), ...)
```

如果 `KeyTransformIn` 未正确注册到 `KeyTransform`，则 `get_lookup('in')` 找不到自定义 lookup，回退到默认 `In`，直接用原始绑定参数比较，与 JSON 提取结果不匹配，导致 0 结果。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | 原始 mutation 用 `get_prep_lookup` 返回空列表，人工痕迹明显，不自然 |
| B | 缺失 | 新建 | 数据集中无 B 组，为 B1 策略设计错误 JSON 路径 mutation |
| C | 高质量 | 保留 | 删除 `rhs = rhs % func`，自然、直接导致 F2P 失败 |
| D | 缺失 | 新建 | 数据集中无 D 组，为 D1 策略设计 Oracle 路径状态重置不完整的 mutation |
| E | 高质量 | 保留 | 添加 `convert_json=False` 参数，符合 E2 策略，语义隐蔽 |

语义浅层：0 个。A 组必须替换，B/D 为新建。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
class KeyTransformIn(lookups.In):
+    def get_prep_lookup(self):
+        return []
     def process_rhs(self, compiler, connection):
```
**分类**：🔴 必须替换
**理由**：`get_prep_lookup` 直接返回空列表，效果是让所有 IN 查询都无法匹配任何值。这是明显的人工痕迹：没有真实开发者会写一个 `return []` 的 `get_prep_lookup`，代码审查中立即暴露。

**最终 mutation**（新设计）：
```diff
-KeyTransform.register_lookup(KeyTransformIn)
+JSONField.register_lookup(KeyTransformIn)
 KeyTransform.register_lookup(KeyTransformExact)
```
**变异语义**：`KeyTransformIn` 被注册到 `JSONField` 而非 `KeyTransform`。`value__key__in` 这类 key transform 查询需要 lookup 注册在 `KeyTransform` 上；注册在 `JSONField` 上的 lookup 只对直接 JSON 字段查询生效，key transform 会回退到默认 `In` 行为（无 JSON 提取），导致所有 F2P 测试失败。代码看起来合理，开发者可能认为注册在字段类上更通用。

---

### Group B — 新建

**最终 mutation**：
```diff
             elif connection.vendor in {'sqlite', 'mysql'}:
-                func = ("JSON_EXTRACT(%s, '$')",) * len(rhs_params)
+                func = ("JSON_EXTRACT(%s, '$.value')",) * len(rhs_params)
```
**变异语义**：将 SQLite/MySQL 的 JSON 提取路径从 `'$'`（根值）改为 `'$.value'`（一个名为 `value` 的子字段）。该路径参考了 Oracle 分支中使用 `'$.value'` 的模式，开发者可能误认为统一路径更一致。实际上 `'$.value'` 会提取 JSON 对象的 `value` 键，对于绑定参数中的 JSON 字符串根本不存在此键，所以 `JSON_EXTRACT` 返回 NULL，导致所有 IN 比较失败。

---

### Group C — 保留

**原 mutation**：
```diff
             elif connection.vendor in {'sqlite', 'mysql'}:
                 func = ("JSON_EXTRACT(%s, '$')",) * len(rhs_params)
-            rhs = rhs % func
         return rhs, rhs_params
```
**分类**：🟢 保留
**理由**：删除 `rhs = rhs % func` 导致 format 字符串从未被填充，最终返回的 `rhs` 仍包含原始 `%s` 占位符。这会使 MySQL/SQLite 的 IN 查询直接用字面 JSON 字符串值与 JSON_EXTRACT 提取的结果比较，匹配失败。这是自然的开发者遗漏：写完所有 vendor 逻辑后忘记应用替换步骤。

**变异语义**：RHS 的 `%s` 占位符虽然被绑定参数填充，但没有被包裹在 JSON_EXTRACT 里，导致 SQLite/MySQL 的比较是在原始 JSON 字符串格式（带引号）与 JSON 提取值（不带引号）之间进行，匹配失败。

---

### Group D — 新建

**最终 mutation**：
```diff
                 func = tuple(func)
-                rhs_params = ()
             elif connection.vendor == 'mysql' and connection.mysql_is_mariadb:
```
**变异语义**：在 Oracle 路径中，`rhs_params` 应在生成 `func` 后清空为空元组，因为 Oracle 会将参数值内联进 SQL 字符串（`JSON_QUERY('{"value": 14}', '$.value')`）而非使用绑定参数。若不清空 `rhs_params`，原始绑定参数仍会被传入，导致 Oracle 数据库收到的绑定参数数量超过 SQL 中的占位符数量（Oracle 的 `rhs` 不含 `%s`），引发数据库错误或参数绑定错误。代码看起来像是开发者认为 `rhs_params` 会在后续被覆盖，实际上 Oracle 分支需要主动清空。

---

### Group E — 保留

**原 mutation**：
```diff
 class KeyTransformIn(lookups.In):
-    def process_rhs(self, compiler, connection):
+    def process_rhs(self, compiler, connection, convert_json=False):
         rhs, rhs_params = super().process_rhs(compiler, connection)
-        if not connection.features.has_native_json_field:
+        if convert_json and not connection.features.has_native_json_field:
```
**分类**：🟢 保留
**理由**：符合 E2 策略（隐式行为变为显式参数），语义隐蔽。JSON 转换被隐藏在 `convert_json=False` 后面，没有任何调用者传递该参数，所以 SQLite/MySQL/Oracle 的 JSON 提取永远不会发生。代码看起来像是为了灵活性而添加的参数，实际上破坏了默认行为。

**变异语义**：`process_rhs` 签名多了一个 `convert_json=False` 参数，但所有实际调用路径（Django ORM 内部）都不传递此参数，所以 `convert_json` 始终为 False，JSON 转换逻辑从不执行，所有 F2P 测试失败。

## 新设计 Mutation 说明

### Group A（替换原有 🔴 必须替换）

基于分析：`KeyTransformIn` 需要注册在 `KeyTransform` 而非 `JSONField` 上。在 Django ORM 中，`value__key__in` 解析时，经过 `KeyTransform.get_lookup('in')` 查找，如果 lookup 只注册在 `JSONField` 上则无法找到，会回退到默认 `In`。这模拟了真实开发者在注册位置时犯的理解错误：误以为注册在字段类（JSONField）上会被所有变换继承，实则不然。

### Group B（新建）

基于对 Oracle 分支路径字符串的分析：Oracle 使用 `'$.value'` 作为路径，而 SQLite/MySQL 使用 `'$'`（根值）。开发者可能误参考了 Oracle 的路径格式，将 `'$'` 改为 `'$.value'`，认为两者应该统一。实际上 `JSON_EXTRACT(json_string, '$.value')` 对于直接 JSON 值（如 `"14"` 或 `14`）会返回 NULL，因为根级别没有 `value` 子键。

### Group D（新建）

基于 Oracle 路径的 state reset 分析：Oracle 将绑定值内联进 SQL（通过 `JSON_QUERY`/`JSON_VALUE` 格式字符串），不需要绑定参数，所以需要 `rhs_params = ()` 清空。若忘记此步骤，原有绑定参数被保留，而 Oracle 生成的 `rhs` 字符串（如 `(JSON_VALUE('{"value":14}', '$.value'), JSON_VALUE('{"value":15}', '$.value'))`）不含任何 `%s` 占位符，最终 SQL 执行时绑定参数数量不匹配，引发 Oracle 数据库错误。此错误在 MySQL/SQLite 下不触发，仅影响 Oracle 路径。
