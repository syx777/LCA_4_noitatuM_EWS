# django__django-17029

## 问题背景

`Apps.clear_cache()` 没有清理 `get_swappable_settings_name` 的缓存。`get_swappable_settings_name` 是个 `functools.cache`（`_lru_cache_wrapper`），`clear_cache()` 文档声称"清理所有内部缓存"，但漏了这一项。django-stubs 用 `clear_cache()` 在多次 mypy 运行间重置状态时发现该缓存未被清。Golden patch 在 `clear_cache` 开头加一行 `self.get_swappable_settings_name.cache_clear()`。

## Golden Patch 语义分析

```python
def clear_cache(self):
    """Clear all internal caches, for methods that alter the app registry."""
    self.get_swappable_settings_name.cache_clear()   # ← 新增
    self.get_models.cache_clear()
    ...
```
核心语义：**`clear_cache()` 必须调用 `self.get_swappable_settings_name.cache_clear()` 真正清空该 lru_cache**。关键点：方法名是 `cache_clear`（而非 `cache_info` 等只读方法）、必须实际调用（带括号）、不能门控在条件后。清空后 `cache_info().currsize` 应为 0。

F2P 测试 `AppsTests.test_clear_cache`：先调 `get_swappable_settings_name` 与 `get_models` 填充缓存，调 `clear_cache()` 后断言两者的 `cache_info().currsize == 0`。

## 调用链分析

`clear_cache()` → `self.get_swappable_settings_name.cache_clear()`（清 lru_cache）+ `self.get_models.cache_clear()` + 遍历模型 `_expire_cache`。`get_swappable_settings_name` 由 `functools.cache` 装饰，`.cache_clear()` 是其包装方法。删除该调用、注释掉、改用只读方法、漏括号、或门控在默认关闭开关后，都会让 swappable 缓存残留、currsize 非 0。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | 整行替换为注释，cache 不被清 |
| B | 🟢 高质量 | 保留 | 用 `#` 注释掉调用行 |
| C | 🔴 必须替换 | 替换 | 原 C 与 A/D 相同；改为 `cache_info()`（只读不清） |
| D | 🔴 必须替换 | 替换 | 原 D 与 A 字节相同；改为 `cache_clear` 漏括号 |
| E | 🟢 高质量 | 保留 | cache_clear 藏到 `clear_swappable` 开关后 |

原始 A、D 字节完全相同（都删除该行），且只有"删行"一种机制（A/B/D 趋同）。保留 A（删行→注释占位）、B（# 注释），重做 C（`cache_info()` 调错方法）、D（`cache_clear` 漏括号），保留 E（开关）。

## 各组 Mutation 分析

### Group A — 保留（D2 状态：删除清理调用）
```diff
-        self.get_swappable_settings_name.cache_clear()
+        # (cache clear removed)
```
**变异语义**：把 `cache_clear()` 调用整行替换为注释占位，`clear_cache` 不再清 `get_swappable_settings_name` 缓存，还原原 bug。`cache_info().currsize` 非 0，F2P 失败。保留。

### Group B — 保留（B2 注释掉调用）
```diff
-        self.get_swappable_settings_name.cache_clear()
+#        self.get_swappable_settings_name.cache_clear()
```
**变异语义**：用 `#` 把调用行整行注释掉（行首 `#` 顶格、保留原缩进文本）。效果同删除——swappable 缓存不被清，currsize 非 0。模拟"调试时临时注释掉某行、忘了恢复"。与 A（替换为说明性注释）形式不同但都使该行失效。F2P 失败。保留。

### Group C — 替换（A1 接口契约：调错方法 cache_info）
**原**：与 A/D 相同（删除该行）。
**最终 mutation**：
```diff
-        self.get_swappable_settings_name.cache_clear()
+        self.get_swappable_settings_name.cache_info()
```
**变异语义**：`cache_clear()` 误写成 `cache_info()`——调用了同一 lru_cache 对象的另一个方法。`cache_info()` 只返回缓存统计（hits/misses/currsize），**不清空缓存**。代码合法、不报错，但缓存依旧 → `currsize` 非 0。模拟"调成了 lru_cache 的另一个相似方法名"。比删行隐蔽——看着确实在操作那个缓存对象。F2P 失败。重做为 C。

### Group D — 替换（C1 值：漏括号 cache_clear）
**原**：与 A 字节相同（删除该行）。
**最终 mutation**：
```diff
-        self.get_swappable_settings_name.cache_clear()
+        self.get_swappable_settings_name.cache_clear
```
**变异语义**：`cache_clear()` 漏了调用括号 `()`，只是取到方法对象（一个绑定方法引用）然后丢弃，**从未执行**。这是合法表达式语句（求值后丢弃，不报错），但缓存未被清。模拟"漏写调用括号、误以为属性访问就会触发清理"。比 A/C 更隐蔽——方法名都对，只差一对括号。F2P 失败。重做为 D。

### Group E — 保留（E2 隐式→显式开关）
```diff
-    def clear_cache(self):
+    def clear_cache(self, clear_swappable=False):
...
-        self.get_swappable_settings_name.cache_clear()
+        if clear_swappable:
+            self.get_swappable_settings_name.cache_clear()
```
**变异语义**：给 `clear_cache` 加 `clear_swappable` 参数（默认 False），swappable 缓存清理只在该开关开启时执行。所有现有调用方（包括 F2P 里的 `apps.clear_cache()`）都不传该参数 → 缓存不被清，currsize 非 0。模拟"把 swappable 缓存清理做成可配置、默认却关掉"。保留。

## 新设计 Mutation 说明

原始 A、D 字节完全相同（删除 cache_clear 行），且 A/B/D 都是"使该行失效"的同类机制。本次保留 A（删行→注释占位）、B（`#` 注释掉），把与 A 重复的 C、D 重做为：C（`cache_info()` 调错方法、只读不清）、D（`cache_clear` 漏括号、取引用不调用），保留 E（`clear_swappable` 默认关闭开关）。五组覆盖"删行 / 注释掉 / 调错方法 / 漏括号 / 默认关闭开关"五个角度——全部令 swappable 缓存残留、currsize 非 0。全部实测（Python 3.11/Django 5.0）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
