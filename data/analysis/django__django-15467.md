# django__django-15467

## 问题背景

当 `ModelAdmin` 给某外键字段配置了 `radio_fields` 时，会丢弃用户在 `formfield_for_foreignkey` 里传入的自定义 `empty_label`，强制用 `_("None")`。Golden patch 改为：若 `db_field.blank`，则用 `kwargs.get("empty_label", _("None"))`——优先尊重调用方传入的 `empty_label`，没有才用默认 `_("None")`。

## Golden Patch 语义分析

```python
kwargs["empty_label"] = (
    kwargs.get("empty_label", _("None")) if db_field.blank else None
)
```
核心语义：**radio 字段的 empty_label 应优先取调用方/`formfield_overrides` 已放入 kwargs 的值，仅在缺失时回退到 `_("None")`**；且仅当 `db_field.blank` 为真时才设 empty_label，否则为 None。关键是 `kwargs.get("empty_label", _("None"))`——用 `get` 带默认值实现"尊重已有、否则默认"。

F2P 测试 `AdminFormfieldForDBFieldTests.test_radio_fields_foreignkey_formfield_overrides_empty_label`：`formfield_overrides = {ForeignKey: {"empty_label": "Custom empty label"}}` + radio_fields，断言生成的 formfield `empty_label == "Custom empty label"`。

## 调用链分析

`formfield_for_dbfield` → `formfield_for_foreignkey`：radio 字段分支里设置 `kwargs["empty_label"]`。`formfield_overrides` 的值会被合并进 `kwargs`，故 `kwargs.get("empty_label")` 能拿到自定义值。任何丢弃/覆盖该自定义值、或反转 `db_field.blank` 判断的改动都会让 F2P 失败。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 把 `kwargs.get` 移到 else 分支，blank 时反而强制 `_("None")`，丢弃自定义值 |
| B | 🟢 高质量 | 保留 | `db_field.blank`→`not db_field.blank`，条件反转 |
| C | 🔴 必须替换 | 替换 | 原 B=C 重复（同 `not db_field.blank`）；改为 `kwargs.get(...) and _("None")` 短路丢值 |
| D | 🟢 高质量 | 保留 | 去掉 `kwargs.get`，硬编码 `_("None")`，丢弃自定义值 |
| E | 🟢 高质量 | 保留 | 把"尊重 kwargs"藏到默认关闭的 `respect_formfield_overrides` 开关后 |

原 5 组齐全但 B=C 重复（都 `not db_field.blank`）。替换 C 为不同机制。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：分支放错）
```diff
-                kwargs["empty_label"] = (
-                    kwargs.get("empty_label", _("None")) if db_field.blank else None
-                )
+                kwargs["empty_label"] = _("None") if db_field.blank else kwargs.get("empty_label")
```
**变异语义**：把 `kwargs.get(...)` 和 `_("None")` 的位置对调——blank 为真时强制 `_("None")`（丢弃自定义值），blank 为假时才取 `kwargs.get`。本测试字段 blank=True，故拿到 `_("None")` 而非 "Custom empty label"，F2P 失败。模拟三元表达式两个分支写反。保留。

### Group B — 保留（B3 条件反转）
```diff
                 kwargs["empty_label"] = (
-                    kwargs.get("empty_label", _("None")) if db_field.blank else None
+                    kwargs.get("empty_label", _("None")) if not db_field.blank else None
                 )
```
**变异语义**：把 `if db_field.blank` 反转为 `if not db_field.blank`。blank=True 的字段落到 else→empty_label=None，自定义值丢失，F2P 失败。保留。

### Group C — 替换（B-逻辑：and 短路丢值）
**原**：与 B 重复（`not db_field.blank`）。
**最终 mutation**：
```diff
                 kwargs["empty_label"] = (
-                    kwargs.get("empty_label", _("None")) if db_field.blank else None
+                    kwargs.get("empty_label") and _("None") if db_field.blank else None
                 )
```
**变异语义**：把 `kwargs.get("empty_label", _("None"))` 换成 `kwargs.get("empty_label") and _("None")`。当自定义 empty_label 存在（真值）时，`X and _("None")` 求值为 `_("None")`——自定义值被 `and` 短路丢弃，反而总是 `_("None")`。模拟"想用 and 做默认值、却搞反了短路语义"的运算符误用，与 B（条件反转）机制不同。

### Group D — 保留（D-去 kwargs.get 硬编码）
```diff
                 kwargs["empty_label"] = (
-                    kwargs.get("empty_label", _("None")) if db_field.blank else None
+                    _("None") if db_field.blank else None
                 )
```
**变异语义**：直接去掉 `kwargs.get`，blank 时硬编码 `_("None")`——完全无视调用方传入的 empty_label。这是 golden 修复的直接逆操作（还原原 bug）。保留。

### Group E — 保留（E2 隐式→显式参数）
```diff
                 kwargs["empty_label"] = (
-                    kwargs.get("empty_label", _("None")) if db_field.blank else None
+                    (kwargs.get("empty_label", _("None")) if getattr(self, "respect_formfield_overrides", False) else _("None")) if db_field.blank else None
                 )
```
**变异语义**：把"尊重 kwargs 的 empty_label"藏到 `respect_formfield_overrides` 开关后，默认 False。于是默认情况下 blank 字段仍强制 `_("None")`，自定义值被忽略。模拟"把修复做成可配置、默认却关掉"的隐式行为退化。保留。

## 新设计 Mutation 说明

原 5 组齐全但 B、C 完全重复（都把 `db_field.blank` 改成 `not db_field.blank`）。本次保留 A（分支对调）、B（条件反转）、D（硬编码 `_("None")`）、E（开关默认关闭），把重复的 C 改为 `kwargs.get("empty_label") and _("None")`（`and` 短路丢弃自定义值）。五组覆盖"分支位置 / 条件反转 / and 短路 / 去 get 硬编码 / 默认关闭开关"五个不同角度。全部实测：golden 通过、变异令 F2P（`test_radio_fields_foreignkey_formfield_overrides_empty_label`）失败、`base→golden→test_patch` 后干净应用。
