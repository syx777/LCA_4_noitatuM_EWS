# django__django-16667

## 问题背景

`SelectDateWidget` 可被用户输入触发 `OverflowError` 导致服务器 500。表单校验时 `value_from_datadict` 用用户提供的 y/m/d 构造 `datetime.date(int(y), int(m), int(d))`；当年份超大（> sys.maxsize）时 `datetime.date(...)` 抛 `OverflowError: Python int too large to convert to C long`，未被捕获。原代码只 `except ValueError`。Golden patch 新增 `except OverflowError: return "0-0-0"`，把溢出转成伪日期字符串 `"0-0-0"`（后续 DateField 校验会判其为无效日期并报友好错误，而非崩溃）。

## Golden Patch 语义分析

```python
try:
    date_value = datetime.date(int(y), int(m), int(d))
except ValueError:
    return "%s-%s-%s" % (y or 0, m or 0, d or 0)
except OverflowError:
    return "0-0-0"
return date_value.strftime(input_format)
```
核心语义：**`datetime.date()` 对超大整数抛 `OverflowError`（不同于 `ValueError`），必须单独捕获并返回一个无效但安全的日期串 `"0-0-0"`**。返回 `"0-0-0"` 而非崩溃——它是个不合法日期，下游 `DateField.clean` 会把它解析失败并产生 "Enter a valid date." 校验错误（用户友好），整个 `is_valid()` 返回 False 而非 500。捕获的异常类型（OverflowError）和返回的哨兵值（`"0-0-0"`）都是修复要点。

F2P 测试：`DateFieldTest.test_form_field`（超大 month 输入断言 `is_valid()` False、错误是 "Enter a valid date."）、`test_datefield_1`（`f.clean("0-0-0")` 抛 "Enter a valid date."）、`SelectDateWidgetTest.test_value_from_datadict`（超大 year 输入断言返回 `"0-0-0"`）。

## 调用链分析

`form.is_valid()` → `SelectDateWidget.value_from_datadict(data, files, name)` 取 y/m/d，`datetime.date(int(y), int(m), int(d))`。超大值 → OverflowError。golden 的 `except OverflowError: return "0-0-0"` 拦截，返回伪日期字符串。该字符串传给 `DateField.clean` → 解析为无效日期 → ValidationError("Enter a valid date.") → is_valid() False。若不捕获 OverflowError（或捕获错类型、返回错哨兵）则崩溃或行为不符。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `except OverflowError`→`except TypeError`，OverflowError 不被捕获仍崩溃 |
| B | 🟢 高质量 | 保留 | 删除整个 `except OverflowError` 块，还原崩溃 |
| C | ➕ 补充 | 新增 | 捕获 OverflowError 但返回 `""` 而非 `"0-0-0"`（错误哨兵） |
| D | ➕ 补充 | 新增 | 捕获 OverflowError 但返回 `None`（widget 误判为无数据） |
| E | 🟢 高质量 | 保留（重做）| OverflowError 处理藏到默认关闭开关后，否则 re-raise |

原 B、C、D 字节完全相同（删除 `except OverflowError` 块），A 距其不同。保留 A、B，补充 C、D，重做 E。本 patch 是单个 except 块，五组围绕"捕获什么/返回什么"分化。

## 各组 Mutation 分析

### Group A — 保留（A1 接口契约：捕获错类型）
```diff
-            except OverflowError:
+            except TypeError:
                 return "0-0-0"
```
**变异语义**：捕获 `TypeError` 而非 `OverflowError`。超大整数构造 date 抛的是 OverflowError，不匹配 `except TypeError` → 异常向上冒泡 → 500 崩溃（还原原 bug）。模拟"捕获了错误的异常类型"。保留。

### Group B — 保留（B2 删除 except 块）
```diff
-            except OverflowError:
-                return "0-0-0"
             return date_value.strftime(input_format)
```
**变异语义**：删除整个 `except OverflowError` 块，OverflowError 不被捕获 → 崩溃。直接还原原 bug。保留。

### Group C — 补充（C1 值：错误哨兵 ""）
```diff
             except OverflowError:
-                return "0-0-0"
+                return ""
```
**变异语义**：捕获了 OverflowError（不崩溃了），但返回空字符串 `""` 而非 `"0-0-0"`。`""` 传给 DateField 的处理与 `"0-0-0"` 不同——可能被当作"空值/未填"而非"无效日期"，导致校验错误信息不符或 is_valid 结果错。`SelectDateWidgetTest.test_value_from_datadict`（断言返回 `"0-0-0"`）失败。模拟"捕获对了、但返回了错误的哨兵值"。

### Group D — 补充（D1 状态：返回 None）
```diff
             except OverflowError:
-                return "0-0-0"
+                return None
```
**变异语义**：捕获 OverflowError 后返回 `None`。`value_from_datadict` 返回 None 表示"该字段无数据"，于是表单认为用户没填 → 可能触发 "required" 错误而非 "Enter a valid date."，或 is_valid 行为不符。`test_value_from_datadict`（期望 `"0-0-0"`）失败。模拟"用 None 表示错误、与伪日期串语义不同"。与 C（空串）都是错误哨兵但行为路径不同（None=无数据 vs ""=空值）。

### Group E — 保留（E2 隐式→显式开关）
**原**：与 B 相同（删 except 块）。
**最终 mutation**：
```diff
             except OverflowError:
-                return "0-0-0"
+                if getattr(self, "handle_overflow", False):
+                    return "0-0-0"
+                raise
```
**变异语义**：捕获 OverflowError 后，只有 `handle_overflow` 开关开启才返回 `"0-0-0"`，否则 `raise` 重新抛出 → 崩溃。默认 `handle_overflow=False` → re-raise → 500（原 bug）。只有显式开启才安全。模拟"把溢出处理做成可配置、默认却关掉（直接重抛）"。重做为 E。

## 新设计 Mutation 说明

原 B、C、D 字节完全相同（删除 `except OverflowError` 块），实际只有"捕获错类型"（A）和"删 except 块"两种机制、仅 4 个 mutation。本次保留 A（`except TypeError` 捕获错类型）、B（删 except 块），补充 C（捕获但返回 `""` 错误哨兵）、D（捕获但返回 `None` 误判无数据），重做 E（`handle_overflow` 默认关闭、否则 re-raise）。本 patch 是单个 except 块，五组围绕"捕获什么异常 / 删除处理 / 返回什么哨兵 / 默认关闭开关"分化为五个角度——A/B/E 让 OverflowError 仍崩溃，C/D 不崩溃但返回错误哨兵使校验结果不符。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
