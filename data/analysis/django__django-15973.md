# django__django-15973

## 问题背景

当 M2M 字段的 `through` 中介模型定义在**另一个 app** 时，迁移崩溃 `AttributeError: 'str' object has no attribute '_meta'`。根因在迁移自动检测器 `_get_dependencies_for_foreign_key`：计算 through 模型所在 app 的依赖时，错误地把 `remote_field_model`（M2M 指向的目标模型，如 `variavel.VariavelModel`）传给 `resolve_relation`，而非 `field.remote_field.through`（中介模型，如 `fonte_variavel.FonteVariavelModel`）。于是生成的迁移依赖指向错误的 app，导致 through 模型未在正确顺序创建，后续 `create_model` 拿到字符串形式的 through 引用、访问 `._meta` 崩溃。Golden patch 把传参从 `remote_field_model` 改为 `field.remote_field.through`。

## Golden Patch 语义分析

```python
if getattr(field.remote_field, "through", None):
    through_app_label, through_object_name = resolve_relation(
        field.remote_field.through,   # 原为 remote_field_model
        app_label,
        model_name,
    )
    dependencies.append((through_app_label, through_object_name, None, True))
```
核心语义：**through 中介模型的依赖必须根据 `field.remote_field.through`（中介模型本身）解析，而非 M2M 的目标模型 `remote_field_model`**。两者可能在不同 app：目标模型在 `variavel`，through 在 `fonte_variavel`。用错对象会让 autodetector 把 through 的依赖错挂到目标 app 上，破坏迁移拓扑排序，through 模型不会被先建。

F2P 测试 `AutodetectorTests.test_create_with_through_model_separate_apps`：Author(authors app) 的 M2M 指向 Publisher(testapp)，through 是 Contract(contract app)。断言 authors 的 AddField 迁移依赖包含 `("contract", "auto_1")` 与 `("testapp", "auto_1")`，即 through 与目标各自的 app 都被正确依赖。

## 调用链分析

`MigrationAutodetector` 检测变更时对每个 FK/M2M 调 `_get_dependencies_for_foreign_key(app_label, model_name, field, project_state)`。先据 `remote_field_model` 算目标依赖，再（若有 through）据 through 算中介依赖并 append。`resolve_relation(relation, app_label, model_name)` 把 `"app.Model"` 或模型类解析成 `(app_label, object_name)`。传 `remote_field_model` 会解析出目标 app；传 `field.remote_field.through` 才解析出中介 app。结果依赖列表决定迁移生成与排序。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `field.remote_field.through`→`remote_field_model`，直接还原原 bug |
| B | 🟢 高质量 | 保留 | `if through`→`if not through`，条件反转 |
| C | 🟢 高质量 | 保留 | 不调 resolve_relation，直接拆成 `(through, None)` |
| D | 🟢 高质量 | 保留 | 只在 through 与当前 app 同 label 时才 append 依赖 |
| E | 🟢 高质量 | 保留 | through 依赖藏到新增的默认关闭参数 `use_through_model` 后 |

原五组机制各异且均有效，全部保留（仅核验，无需替换/补充）。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：传错对象，还原 bug）
```diff
             through_app_label, through_object_name = resolve_relation(
-                field.remote_field.through,
+                remote_field_model,
                 app_label,
                 model_name,
             )
```
**变异语义**：把 through 依赖解析的输入从中介模型 `field.remote_field.through` 改回 M2M 目标模型 `remote_field_model`。through 在独立 app 时，依赖被错算到目标 app，迁移排序错误，还原原始 `AttributeError`。当 through 与目标恰在同一 app 时不出错（故普通用例可能通过），分离 app 场景失败。保留。

### Group B — 保留（B3 条件反转）
```diff
-        if getattr(field.remote_field, "through", None):
+        if not getattr(field.remote_field, "through", None):
             through_app_label, through_object_name = resolve_relation(
                 field.remote_field.through,
```
**变异语义**：进入 through 依赖分支的条件反转为 `not through`。有 through 时**不**添加其依赖（缺依赖→排序错），无 through 时反而进入分支访问 `field.remote_field.through`（None）抛错。逻辑完全颠倒。保留。

### Group C — 保留（C1 类型/数据形状：跳过 resolve_relation）
```diff
         if getattr(field.remote_field, "through", None):
-            through_app_label, through_object_name = resolve_relation(
-                field.remote_field.through,
-                app_label,
-                model_name,
-            )
+            through_app_label, through_object_name = field.remote_field.through, None
             dependencies.append((through_app_label, through_object_name, None, True))
```
**变异语义**：不调 `resolve_relation`，直接把 `through_app_label` 设为整个 `"app.Model"` 字符串、`through_object_name` 设为 None。依赖元组形如 `("contract.Contract", None, None, True)`，app_label/object_name 未拆分，依赖匹配失败。模拟"以为 through 已是拆好的 (app, name)、省略解析"。保留。

### Group D — 保留（B3 条件：限制 append）
```diff
-            dependencies.append((through_app_label, through_object_name, None, True))
+            if through_app_label == app_label:
+                dependencies.append((through_app_label, through_object_name, None, True))
```
**变异语义**：只在 through 与当前模型**同 app** 时才添加依赖。跨 app（正是 bug 场景）时 `through_app_label != app_label` → 不 append → 缺失依赖 → 迁移排序错误。模拟"误以为只需处理同 app 的 through"。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
-    def _get_dependencies_for_foreign_key(app_label, model_name, field, project_state):
+    def _get_dependencies_for_foreign_key(app_label, model_name, field, project_state, use_through_model=False):
...
-        if getattr(field.remote_field, "through", None):
+        if use_through_model and getattr(field.remote_field, "through", None):
             through_app_label, through_object_name = resolve_relation(
```
**变异语义**：新增参数 `use_through_model`（默认 False），through 依赖只在显式传 True 时才计算。所有现有调用方都不传该参数 → 默认 False → through 依赖永不添加。模拟"把已有行为做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

本实例原五组机制已各不相同且全部有效，无重复、无必须替换项，故全部保留并逐一核验。五组覆盖"传错对象 / 条件反转 / 跳过解析 / 限制同 app append / 默认关闭开关"五个角度，分别作用于解析输入、进入条件、解析方式、append 条件、函数签名五个位置。全部实测：golden 通过、五个变异均令 F2P（`test_create_with_through_model_separate_apps`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
