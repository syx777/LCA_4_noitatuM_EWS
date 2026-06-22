# django__django-14631 Mutation 分析

## 问题背景

Django 工单指出 `BaseForm._clean_fields()` 与 `BaseForm.changed_data` 没有通过 `BoundField` 对象读取字段值，导致代码路径重复，并且存在一个隐蔽缺陷：对于 `disabled=True` 且 `initial` 为可调用对象（callable）的字段，`form._clean_fields()` 得到的清洗值可能与 `form[name].initial` 不一致。

根因在于两条独立路径分别求 initial：
- `_clean_fields()` 旧代码用 `self.get_initial_for_field(field, name)`，它**每次都重新调用** callable，并且**不剥离 microseconds**。
- `BoundField.initial`（`cached_property`）只调用 callable **一次**并缓存，且当 widget 的 `supports_microseconds=False`（`DateTimeField` 默认即是）时会**剥离 microseconds**。

因此对可调用 initial，两者既在"调用次数/时间"上不同，也在"是否含微秒"上不同。

## Golden Patch 语义分析

Golden patch 触及两个文件：
- `django/forms/forms.py`：新增 `_bound_items()` 生成 `(name, bf)`；`_clean_fields()` 改为 `value = bf.initial if field.disabled else bf.data`，FileField 分支改用 `bf.initial`；`changed_data` 委托给 `bf._has_changed()`；删除 `_field_data_value`。
- `django/forms/boundfield.py`：`data` 改用 `_widget_data_value`；新增 `_has_changed()`。

核心修复：disabled 字段的清洗值统一走 `bf.initial`（缓存 + 剥微秒），保证 `cleaned_data[name] == form[name].initial`。

## 调用链分析

`form.cleaned_data` → `full_clean()` → `_clean_fields()` → 对 disabled 字段取 `bf.initial`
→ `BoundField.initial` (`@cached_property`) → `form.get_initial_for_field()`（求值 callable）→ 若 datetime 且 widget 不支持微秒则 `replace(microsecond=0)`。

F2P 测试（`tests/forms_tests/tests/test_forms.py`）用一个 `FakeTime.now`（每次调用 `elapsed_seconds += 1`）作为 callable initial：
- `test_datetime_clean_disabled_callable_initial_microseconds`：断言 `cleaned_data['dt']` 等于剥除微秒后的值。
- `test_datetime_clean_disabled_callable_initial_bound_field`：断言 `cleaned == bf.initial`（同一次缓存求值）。

P2P 中的 `test_form_with_disabled_fields`（静态 date initial）、`test_boundfield_initial_called_once`、`test_initial_datetime_values` 限制了变异空间：任何破坏静态 initial 或破坏缓存语义的改动都会回归 P2P。

## 替换决策总览

| 槽位 | 原 strategy_code 类型 | 原变异 | 分类 | 决策 | 新 strategy_code |
|------|----------------------|--------|------|------|------------------|
| A | A (get_initial_for_field 回退) | `get_initial_for_field(field, name)` | 🔴 与 E 字节级重复（直接回退原 bug） | 替换 | A2 |
| B | B (布尔反转) | `if not field.disabled` | 🔴 破坏 36 个 P2P 测试 | 替换 | B2 |
| C | C (value 调用) | `else bf.value()` | 🔴 仅破坏 P2P（filefield），不破坏任何 F2P | 替换 | A1 |
| E | E (get_initial_for_field 回退) | `get_initial_for_field(field, name)` | 🔴 与 A 字节级重复 | 替换 | A2 |

四个槽位全部 🔴，必须替换。设计目标：在唯一有效变异面（`_clean_fields` 的 disabled 分支赋值）上实现**正交的失败模式**。

## 各组 Mutation 分析

- **原 A / 原 E**：两条 diff 完全字节相同，都是把 `bf.initial` 直接换回 `get_initial_for_field(field, name)`——即原始缺陷的逐字还原。属直接 golden-revert 冗余，且互为重复，必须替换。
- **原 B**：`if not field.disabled` 把 disabled 判定整体反转，导致几乎所有普通字段走错分支，36 失败 + 2 错误，严重 P2P 回归，必须替换。
- **原 C**：`bf.value()` 仅改写 **非 disabled** 的 else 分支；而所有 F2P 测试都是 `disabled=True`，故 F2P 全部仍通过，只击穿 `test_filefield_initial_callable`（P2P）。无法让任何 F2P 失败，属无效变异，必须替换。

## 新设计 Mutation 说明

全部落在 `_clean_fields()` 的 disabled 赋值行，但机制各异，形成三类正交失败族：

- **槽位 A（A2，单测失败族）**：`field.get_bound_field(self, name).initial`。`get_bound_field` 新建一个**全新 BoundField**，绕过 form 的 `_bound_fields_cache`，其 `.initial` 会**再次调用** callable（FakeTime 前进）但仍**剥除微秒**。因此微秒测试照常通过，只有 bound-field 一致性测试（`cleaned == bf.initial`）失败。看起来像"统一通过 BoundField 访问"的合理重构，最难察觉。

- **槽位 B（B2，崩溃/ERROR 族）**：`self.initial.get(name, field.initial)`，丢弃了 `get_initial_for_field`/`BoundField` 提供的 callable 求值与空值处理。对可调用 initial，未求值的函数对象被直接传入 `field.clean`，在 `to_python` 深处抛 `AttributeError`，两个 F2P 测试均以 ERROR 形式失败；而静态 initial 的 disabled 测试仍通过，是一处极易被忽略的"省略求值"。

- **槽位 C（A1，双测断言失败族 · 微秒语义）**：`(field.initial() if callable(field.initial) else bf.initial)`，手写内联求值替代 BoundField 委托，**不剥微秒**且 callable 多触发一次。精确复刻文档所述缺陷语义，但写成看似合理的手卷逻辑；只断言"无微秒静态 initial"的测试套件无法发现。

- **槽位 E（A2，双测断言失败族 · 参数键错位）**：`get_initial_for_field(field, self.add_prefix(name))`，把带前缀的 html 名误当作字段键传入（参数语义/键错位）。`get_initial_for_field` 重新求值 callable（前进时间、保留微秒），复现原路径分歧，伪装成无害的"前缀一致性"微调。

C 与 E 同属双测失败族但代码路径与失因不同（内联求值 vs 错误键 + 委托），A 与 B 分别为单测族与崩溃族，整体覆盖三种正交失败机制。
