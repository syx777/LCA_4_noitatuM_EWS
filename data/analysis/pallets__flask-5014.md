# pallets__flask-5014

## 问题背景

给 Blueprint 传空名（`Blueprint("", ...)`）时不报错，但后续行为会出问题（见 #4944）。应在空名时抛 `ValueError`。Golden patch 在 `Blueprint.__init__` 里、已有的 dot 检查之前，加 `if not name: raise ValueError("'name' may not be empty.")`。

## Golden Patch 语义分析

```python
super().__init__(...)

if not name:
    raise ValueError("'name' may not be empty.")

if "." in name:
    raise ValueError("'name' may not contain a dot '.' character.")
```
核心语义：**空名（`not name` 为真——空串、None 等假值）必须抛 `ValueError`**。关键点：用 `not name`（捕获空串和 None）、抛 `ValueError`（与现有 dot 检查一致）、无条件执行。

F2P 测试 `tests/test_blueprints.py::test_empty_name_not_allowed`：`with pytest.raises(ValueError): flask.Blueprint("", __name__)`。

## 调用链分析

`flask.Blueprint("", __name__)` → `Blueprint.__init__` → `if not name: raise ValueError`。守卫的异常类型、判断表达式、是否执行决定空名是否被拦。异常类型错、判断收窄（is None / isinstance）、注释掉、或门控开关，都会让空名不抛 ValueError。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `raise ValueError`→`raise TypeError`，异常类型错 |
| B | 🟢 高质量 | 保留 | `if not name`→`if name is None`，漏掉空串 |
| C | ➕ 补充 | 重做 | `if not isinstance(name, str)`，空串是 str 漏掉 |
| D | 🟢 高质量 | 保留 | 注释掉守卫（还原 bug） |
| E | 🟢 高质量 | 重做 | 守卫藏到 _strict_name 开关后 |

原始 A==E（TypeError）、B==C（is None）。保留 A（TypeError）、B（is None）、D（注释），重做 C（isinstance 检查）、E（默认关闭开关）。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：异常类型错）
```diff
-            raise ValueError("'name' may not be empty.")
+            raise TypeError("'name' may not be empty.")
```
**变异语义**：空名校验抛 `TypeError` 而非 `ValueError`。`Blueprint('')` 确实抛异常，但 F2P 的 `pytest.raises(ValueError)` 不匹配 TypeError（TypeError 不是 ValueError 子类），测试失败。模拟"抛了错误的异常类型"。保留。

### Group B — 保留（B3 条件收窄：is None）
```diff
-        if not name:
+        if name is None:
```
**变异语义**：`if not name`（捕获所有假值）改成 `if name is None`（只捕获 None）。空字符串 `''` 不是 None，不触发校验，`Blueprint('')` 不报错。F2P 期望 ValueError 失败。模拟"守卫只判了 None、漏掉空串"。保留。

### Group C — 重做（C1 类型：isinstance 检查）
**原**：与 B 相同（`if name is None`）。
**最终 mutation**：
```diff
-        if not name:
+        if not isinstance(name, str):
```
**变异语义**：`if not name` 改成 `if not isinstance(name, str)`——空字符串 `''` 是 str，`isinstance` 为真、`not` 为假，不触发校验，`Blueprint('')` 通过。把"空值校验"误改成"类型校验"，漏掉合法类型的空值。F2P 失败。与 B（is None）不同——这里检查的是类型而非身份。重做为 C。

### Group D — 保留（B2 注释掉守卫）
```diff
-        if not name:
-            raise ValueError("'name' may not be empty.")
+        # if not name:
+        #     raise ValueError("'name' may not be empty.")
```
**变异语义**：注释掉整个空名校验——还原原 bug：空名 Blueprint 不报错。F2P 期望 ValueError 失败。保留。

### Group E — 重做（E2 隐式→显式开关）
**原**：与 A 相同（TypeError）。
**最终 mutation**：
```diff
-        if not name:
+        if not name and getattr(self, "_strict_name", False):
             raise ValueError("'name' may not be empty.")
```
**变异语义**：空名校验追加 `and getattr(self, "_strict_name", False)` 开关（默认无该属性→False）。默认 `not name and False` 恒假 → 空名不报错。只有显式设 `_strict_name=True` 才校验。模拟"把空名校验做成可配置、默认却关掉"。重做为 E。

## 新设计 Mutation 说明

原始 A==E（都 TypeError）、B==C（都 `is None`），实际只有"异常类型错 / is None / 注释"三种机制。本次保留 A（TypeError）、B（is None 漏空串）、D（注释还原 bug），重做 C（`isinstance(name, str)` 类型检查漏空串）、E（`_strict_name` 默认关闭开关）。五组覆盖"异常类型 / is None / isinstance 类型 / 注释守卫 / 默认关闭开关"五个角度——全部令空名 Blueprint 不抛 ValueError。全部实测（Python 3.11/Flask 2.2.3 + werkzeug 2.2.3，ng311 环境）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
