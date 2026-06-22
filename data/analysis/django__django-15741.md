# django__django-15741

## 问题背景

`django.utils.formats.get_format` 不接受 lazy 字符串参数。当模板过滤器写 `some_date|date:_('Y-m-d')`（`_` 返回 lazy proxy）时，`format_type` 是 lazy 对象，后续 `getattr(module, format_type, None)` 因 `getattr` 要求属性名是真正的 `str` 而抛 `TypeError: getattr(): attribute name must be string`。Golden patch 在使用 `format_type` 前加一行 `format_type = str(format_type)` 把 lazy 强制求值为 str。

## Golden Patch 语义分析

```python
if use_l10n and lang is None:
    lang = get_language()
format_type = str(format_type)  # format_type may be lazy.
cache_key = (format_type, lang)
```
核心语义：**在把 `format_type` 用作 cache key、以及传给 `getattr(module, format_type)` 之前，必须先 `str()` 求值 lazy 代理**。这一行既保证 cache_key 用真实字符串（避免 lazy 对象哈希/相等问题），又保证后续 `getattr` 拿到真正 str。位置在 `cache_key` 赋值之前是关键——它要覆盖 cache 查找和 getattr 两条路径。

F2P 测试两个：`FormattingTests.test_get_format_lazy_format`（`get_format(gettext_lazy("DATE_FORMAT")) == "N j, Y"`）与 `DateTests.test_date_lazy`（模板 `{{ t|date:_("H:i") }}` 渲染不报错）。

## 调用链分析

`get_format(format_type, lang, use_l10n)` 先规整 `use_l10n`/`lang`，然后 `cache_key = (format_type, lang)` 查缓存；未命中则 `for module in get_format_modules(lang): val = getattr(module, format_type, None)`。lazy 的 `format_type` 在 `getattr` 处直接触发 TypeError。`str(format_type)` 必须在这两处使用前完成；若只在其中一处求值、或求值结果未回写到 `format_type`，则另一条路径仍拿到 lazy 对象。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原 A=C=D 完全相同（注释掉该行）；保留为"注释" |
| B | 🔴 必须替换 | 替换 | 原与 A 重复；改为 `if isinstance(str)` 守卫使转换永不生效 |
| C | 🔴 必须替换 | 替换 | 原与 A 重复；改为只在 cache_key 局部 str()，getattr 仍 lazy |
| D | 🔴 必须替换 | 替换 | 原与 A 重复；改为死调用（结果不回写） |
| E | 🟢 高质量 | 保留 | 删除整行，`format_type` 全程 lazy |

原 A/C/D 三组字节完全相同（注释掉 `str()` 行），E 删行。把 A 保留为"注释"形态、B/C/D 重做为三种不同机制、E 保留删行。

## 各组 Mutation 分析

### Group A — 替换（D2 死代码注释）
**原 mutation**（A=C=D）：注释掉 `format_type = str(format_type)`。
**最终 mutation**：
```diff
-    format_type = str(format_type)  # format_type may be lazy.
+    # format_type = str(format_type)  # format_type may be lazy.
```
**变异语义**：把转换行整体注释。`format_type` 全程保持 lazy，`getattr` 抛 TypeError。形式是"调试时临时注释、忘了恢复"的死代码。两个 F2P 均失败。

### Group B — 替换（B3 条件守卫使转换失效）
```diff
-    format_type = str(format_type)  # format_type may be lazy.
+    if isinstance(format_type, str):
+        format_type = str(format_type)
```
**变异语义**：把转换包在 `if isinstance(format_type, str)` 守卫里。lazy 代理**不是** `str` 实例（是 `__proxy__`），故守卫为 False，转换被跳过，lazy 对象原样进入 getattr。看似"已经是 str 才需转"（其实多余但无害的判断），实则恰好排除了唯一需要转换的 lazy 情形。模拟"加了个看似合理实则反效果的类型守卫"。

### Group C — 替换（C1 局部 str 化：getattr 仍 lazy）
```diff
-    format_type = str(format_type)  # format_type may be lazy.
-    cache_key = (format_type, lang)
+    cache_key = (str(format_type), lang)
```
**变异语义**：只在 `cache_key` 元组里就地 `str()`，但不回写 `format_type` 变量。cache 查找用了真实 str（看似修复了一半），但后续 `getattr(module, format_type)` 仍拿到 lazy 对象 → TypeError。模拟"只在眼前的 cache_key 处理了 lazy、漏了下游的 getattr"。

### Group D — 替换（D1 状态：死调用不回写）
```diff
-    format_type = str(format_type)  # format_type may be lazy.
+    str(format_type)  # format_type may be lazy.
```
**变异语义**：调用了 `str(format_type)` 但丢弃返回值、没有赋回 `format_type`。`str()` 对 lazy 求值后产生的 str 被立即丢弃，`format_type` 仍是 lazy。代码行几乎与 golden 一样、只少了 `format_type = ` 前缀，极易被审查者忽略。两个 F2P 均失败。

### Group E — 保留（B2 删除整行）
```diff
-    format_type = str(format_type)  # format_type may be lazy.
     cache_key = (format_type, lang)
```
**变异语义**：直接删除转换行，`format_type` 全程 lazy，还原原 bug。保留。

## 新设计 Mutation 说明

原实例 A/C/D 三组字节完全相同（都注释掉 `str()` 行），实际只有"注释"与"删行"（E）两种机制。本次保留 E（删行），把 A 定为"注释"形态，B/C/D 重做为三种不同机制：B 用 `isinstance(str)` 守卫使转换永不对 lazy 生效、C 只在 cache_key 局部 str() 而漏掉 getattr、D 死调用结果不回写。五组覆盖"注释 / 类型守卫失效 / 局部转换 / 死调用 / 删行"五个角度。全部实测：golden 通过、五个变异均令两个 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
