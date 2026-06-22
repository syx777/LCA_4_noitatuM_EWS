# django__django-13512

## 问题背景

Django Admin 中显示 `JSONField` 值时，非 ASCII 字符（如中文、日文、emoji 等）会被 `\uXXXX` 转义，而不是显示原始 UTF-8 字符。

根本原因有两处：

1. **`admin/utils.py`** — `display_for_field()` 对 `JSONField` 调用 `field.get_prep_value(value)`，而 `get_prep_value` 内部调用 `json.dumps` 时没有设置 `ensure_ascii=False`，导致非 ASCII 字符被转义。

2. **`forms/fields.py`** — `JSONField.prepare_value()` 的 `json.dumps` 调用同样没有 `ensure_ascii=False`。

修复：
- `admin/utils.py`：改用 `json.dumps(value, ensure_ascii=False, cls=field.encoder)` 直接序列化，加 `import json`
- `forms/fields.py`：为 `json.dumps` 添加 `ensure_ascii=False`

## Golden Patch 语义分析

**`admin/utils.py`**：
```python
# 修复前
return field.get_prep_value(value)  # 内部 ensure_ascii=True，非ASCII被转义
# 修复后
return json.dumps(value, ensure_ascii=False, cls=field.encoder)  # 直接输出UTF-8
```

**`forms/fields.py`**：
```python
# 修复前
return json.dumps(value, cls=self.encoder)  # 默认 ensure_ascii=True
# 修复后
return json.dumps(value, ensure_ascii=False, cls=self.encoder)
```

`ensure_ascii=False` 允许 `json.dumps` 直接输出非 ASCII 字符（UTF-8），而非将其转换为 `\uXXXX` 转义序列。

## 调用链分析

```
Admin list view: JSONField 列显示
  → display_for_field(value, field, empty_value_display)
  → isinstance(field, models.JSONField) → True
  → json.dumps(value, ensure_ascii=False, cls=field.encoder)  ← 修复

Admin form widget: JSONField 表单输入值显示
  → forms.JSONField.prepare_value(value)
  → json.dumps(value, ensure_ascii=False, cls=self.encoder)  ← 修复
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 语义浅层 | 保留 | forms 中漏掉 ensure_ascii=False，只影响表单展示 |
| B | 语义浅层 | 保留 | admin utils 中漏掉 ensure_ascii=False，只影响列表视图 |
| C | 高质量 | 保留 | 两处都漏掉 ensure_ascii=False，全面失效 |
| D | 必须替换（重复A） | 替换 | 原与A相同，替换为 admin utils 中恢复 get_prep_value |
| E | 高质量 | 保留 | ensure_ascii=True，明确使用错误参数 |

语义浅层 2 个（A、B），按规则 floor(2/2)=1 个最弱者替换。A 和 D 原本相同，将 D 替换为新的 admin utils 变体；A 保留（forms/fields.py 变体，与 B/admin 变体互补）。

## 各组 Mutation 分析

### Group A — 保留

**原 mutation**：
```diff
-        return json.dumps(value, ensure_ascii=False, cls=self.encoder)
+        return json.dumps(value, cls=self.encoder)
```
（仅 `forms/fields.py`）

**分类**：🟡 语义浅层（保留）
**理由**：`prepare_value()` 中去掉 `ensure_ascii=False`，还原到默认的 ASCII 转义行为，表单 widget 中非 ASCII 字符会被 `\uXXXX` 显示。F2P 测试中检查表单渲染输出的非 ASCII 字符断言失败。

---

### Group B — 保留

**原 mutation**：
```diff
-            return json.dumps(value, ensure_ascii=False, cls=field.encoder)
+            return json.dumps(value, cls=field.encoder)
```
（仅 `admin/utils.py`）

**分类**：🟡 语义浅层（保留）
**理由**：`display_for_field()` 中去掉 `ensure_ascii=False`，Admin 列表视图中 JSONField 非 ASCII 字符被转义。与 A 保留一个即可，两者互补（一个影响 forms，一个影响 admin utils）。

---

### Group C — 保留

**原 mutation**：两处都去掉 `ensure_ascii=False`（`forms/fields.py` + `admin/utils.py`）

**分类**：🟢 保留
**理由**：多文件、多位置的修改，覆盖了修复的全部范围。所有 Admin 展示路径（表单和列表）都受影响，所有 F2P 测试失败。模拟了开发者"我知道要加这个参数，但两处都忘了"的场景。

---

### Group D — 替换（原与A重复）

**最终 mutation**：
```diff
-            return json.dumps(value, ensure_ascii=False, cls=field.encoder)
+            return field.get_prep_value(value)
```
（`admin/utils.py`）

**变异语义**：将 `admin/utils.py` 的修复还原为原始的 `field.get_prep_value(value)` 调用。`get_prep_value` 内部使用 `json.dumps(value, cls=self.encoder)`（默认 `ensure_ascii=True`），所以非 ASCII 字符仍被转义。此外，`get_prep_value` 不接受显示定制，而 `json.dumps` 是显式的。这模拟了开发者认为"用现有方法就够了，不需要另起 json.dumps"的错误判断，与 A（只漏 forms 的 ensure_ascii）形成互补。

---

### Group E — 保留

**原 mutation**：
```diff
-        return json.dumps(value, ensure_ascii=False, cls=self.encoder)
+        return json.dumps(value, ensure_ascii=True, cls=self.encoder)
```
（`forms/fields.py`）

**分类**：🟢 保留
**理由**：`ensure_ascii=True` 是明确的错误设置，与 `ensure_ascii=False`（修复）语义相反。这比"漏掉参数"更严格地保证了非 ASCII 的强制转义。开发者可能误以为 `ensure_ascii=True` 才是"安全"的 JSON 输出，或者在 code review 中将 False 改为 True。
