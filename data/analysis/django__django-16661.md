# django__django-16661

## 问题背景

`ModelAdmin.lookup_allowed()` 对"外键作为主键"的场景误判，错误抛出 `DisallowedModelAdminLookup`。当 `OneToOneField(..., primary_key=True)` 时，admin 把"外键作主键"当成了具体继承（concrete inheritance），于是把 `restaurant__place__country` 短路成 `restaurant__country`（实际不存在），导致合法 lookup 被拒。根因：判断某 part 是否应加入 `relation_parts` 时用的是 `field not in prev_field.path_infos[-1].target_fields`——对 FK 主键会误命中 target_fields。Golden patch 改为检查 `field not in model._meta.parents.values()`（真正的继承父链）、`field is not model._meta.auto_field`，并加 `model._meta.auto_field is None or part not in getattr(prev_field, "to_fields", [])` 子条件。

## Golden Patch 语义分析

```python
if not prev_field or (
    prev_field.is_relation
    and field not in model._meta.parents.values()
    and field is not model._meta.auto_field
    and (
        model._meta.auto_field is None
        or part not in getattr(prev_field, "to_fields", [])
    )
):
    relation_parts.append(part)
```
核心语义：**判断 lookup 的某一段是否构成"需要权限校验的关系段"时，应排除的是真正的继承父链（`model._meta.parents.values()`）和自动主键字段（`auto_field`），而非笼统地用 `prev_field.path_infos[-1].target_fields`**。`target_fields` 对 FK 主键场景会误命中，把合法关系段排除掉 → lookup 被拒。新条件用 `parents.values()` 精确表达继承、用 `auto_field` + `to_fields` 处理主键指向。四个 `and` 子条件缺一不可。

F2P 测试 `ModelAdminTests.test_lookup_allowed_foreign_primary`：构造 Country←Place←Restaurant(OneToOne pk)←Waiter，`WaiterAdmin.list_filter` 含 `restaurant__place__country`，断言 `lookup_allowed("restaurant__place__country", ...)`、`__id__exact`、`__name` 均为 True。

## 调用链分析

`lookup_allowed(lookup, value)` 按 `LOOKUP_SEP` 拆段遍历，对每段 `model._meta.get_field(part)` 取 field，用上述布尔条件决定是否 `relation_parts.append(part)`，最终用 relation_parts 判断 lookup 是否在 `list_filter` 允许范围内。条件里 `to_fields` 反转、`or→and`、`parents.values()` 换回 `target_fields`、删 `to_fields` 子条件、或整体藏到开关后，都会让 FK 主键 lookup 被误拒。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `part not in to_fields`→`part in to_fields`，子条件反转 |
| B | 🟢 高质量 | 保留 | `not prev_field or (...)`→`and (...)`，首段永不收集 |
| C | 🟢 高质量 | 保留 | `parents.values()` 换回原 bug 的 `path_infos[-1].target_fields` |
| D | 🟢 高质量 | 保留 | 删除 `auto_field is None or part not in to_fields` 子条件 |
| E | 🟢 高质量 | 重做 | 修复藏到 `fix_fk_pk_lookup` 开关后，默认走 `target_fields`（原 bug） |

原始 C 与 E 都用 `prev_field.path_infos[-1].target_fields`（机制重复）。保留 A/B/C/D，把 E 重做为默认关闭开关（默认回退到 target_fields 比较），与 C 区分开。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：to_fields 子条件反转）
```diff
                 and (
                     model._meta.auto_field is None
-                    or part not in getattr(prev_field, "to_fields", [])
+                    or part in getattr(prev_field, "to_fields", [])
                 )
```
**变异语义**：`part not in to_fields` 反转为 `part in to_fields`。当 part 指向 prev_field 的 to_field（FK 主键场景常见）时，本应"不收集"反成"收集"，反之亦然。FK 主键 lookup 的关系段判定颠倒，`test_lookup_allowed_foreign_primary` 失败。保留。

### Group B — 保留（B3 逻辑运算符：or→and）
```diff
-            if not prev_field or (
+            if not prev_field and (
```
**变异语义**：`not prev_field or (...)` 改成 `not prev_field and (...)`。第一段遍历时 `prev_field is None` → `not prev_field` 为真，原本短路进入收集；改 `and` 后还需后面整个括号为真，但 `prev_field.is_relation`（None 无该属性）会 AttributeError 或为假 → 首段不收集。relation_parts 缺首段，lookup 判定错。保留。

### Group C — 保留（C1 值：换回 target_fields）
```diff
                 prev_field.is_relation
-                and field not in model._meta.parents.values()
+                and field not in prev_field.path_infos[-1].target_fields
```
**变异语义**：把 golden 的 `model._meta.parents.values()` 换回原 bug 的 `prev_field.path_infos[-1].target_fields`。这正是原始 bug 表达式——FK 作主键时 field 误命中 target_fields，关系段被排除，lookup 被短路误拒。`test_lookup_allowed_foreign_primary` 失败。保留。

### Group D — 保留（D1 状态：删 to_fields 子条件）
```diff
                 and field is not model._meta.auto_field
-                and (
-                    model._meta.auto_field is None
-                    or part not in getattr(prev_field, "to_fields", [])
-                )
+                and model._meta.auto_field is None
```
**变异语义**：删除 `model._meta.auto_field is None or part not in to_fields` 整个子条件，只留 `model._meta.auto_field is None`。对有 auto_field 的模型（绝大多数），该项恒为假 → 整个收集条件恒假 → relation_parts 不收集任何关系段，lookup 判定全错。`test_lookup_allowed_foreign_primary` 失败。保留。

### Group E — 重做（E2 隐式→显式开关）
**原**：与 C 相同（用 `path_infos[-1].target_fields`）。
**最终 mutation**：
```diff
             if not prev_field or (
                 prev_field.is_relation
-                and field not in model._meta.parents.values()
-                and field is not model._meta.auto_field
                 and (
-                    model._meta.auto_field is None
-                    or part not in getattr(prev_field, "to_fields", [])
+                    (
+                        field not in model._meta.parents.values()
+                        and field is not model._meta.auto_field
+                        and (
+                            model._meta.auto_field is None
+                            or part not in getattr(prev_field, "to_fields", [])
+                        )
+                    )
+                    if getattr(self, "fix_fk_pk_lookup", False)
+                    else field not in prev_field.path_infos[-1].target_fields
                 )
             ):
```
**变异语义**：把 golden 的完整判定藏到 `self.fix_fk_pk_lookup` 开关后（默认 False）；默认走 `else` 分支的原 bug 表达式 `field not in prev_field.path_infos[-1].target_fields`。默认构造的 ModelAdmin 不设该属性 → 走原 bug → FK 主键 lookup 被误拒。模拟"把 FK 主键修复做成可配置、默认却关掉"。重做为 E，与 C（直接换回 target_fields）机制区分：E 保留了 golden 逻辑但被开关门控。

## 新设计 Mutation 说明

原始 C、E 都用 `prev_field.path_infos[-1].target_fields`（机制重复）。本次保留 A（to_fields 子条件反转）、B（or→and 首段不收集）、C（换回 target_fields 原 bug 表达式）、D（删 to_fields 子条件使条件恒假），把与 C 重复的 E 重做为 `fix_fk_pk_lookup` 默认关闭开关（默认回退 target_fields）。五组覆盖"子条件反转 / 逻辑运算符错 / 换回原 bug 表达式 / 删子条件 / 默认关闭开关"五个角度。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
