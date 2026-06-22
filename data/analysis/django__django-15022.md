# django__django-15022

## 问题背景

Admin changelist 的搜索把搜索词逐个 `qs = qs.filter(...)` 累加，每个词都对多值关系（如 `clientoffice__name`）产生一次新的 JOIN，导致多词搜索时 SQL JOIN 爆炸、查询挂死。Golden patch 改为先把每个搜索词的 OR 子查询收集进 `term_queries`，最后一次性 `queryset.filter(models.Q(*term_queries))` 用单个 AND 组合，避免重复 JOIN。

## Golden Patch 语义分析

```python
term_queries = []
for bit in smart_split(search_term):
    ...
    or_queries = models.Q(*((orm_lookup, bit) for orm_lookup in orm_lookups), _connector=models.Q.OR)
    term_queries.append(or_queries)
queryset = queryset.filter(models.Q(*term_queries))
```
两层语义：
1. **每个搜索词内部**：跨多个 search_field 用 `Q.OR`（任一字段匹配该词即可）。
2. **搜索词之间**：把所有 `or_queries` 收进列表，最后用一个 `Q(*term_queries)`（默认 AND）一次性 filter——所有词必须都匹配，且只产生一次 JOIN。

这同时修复了**性能**（JOIN 数）与**语义**（多值关系上"所有词需匹配同一行"）。

F2P 测试：`test_many_search_terms`（80 个重复词，断言 `sql.count('JOIN')==1`）、`test_related_field_multiple_search_terms`（跨多字段多词，AND 语义下特定组合应返回 0 或 1 行）、`test_multiple_search_fields`（`'Mary Jonathan Duo'` 在 AND 语义下应返回 0）。

## 调用链分析

`ModelAdmin.get_search_results(request, queryset, search_term)` → `construct_search` 为每个 search_field 生成 lookup → 对 `smart_split` 出的每个词构造 `or_queries`（字段间 OR）→ 收集到 `term_queries` → 最终单次 `queryset.filter(models.Q(*term_queries))`（词间 AND）。`term_queries` 的初始化、循环内 append、循环外 filter 三者协同决定最终 SQL 与语义。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原把**词内** `Q.OR`→`Q.AND`，blast radius 巨大（8 失败），不微妙 |
| B | 🟡/🔴 必须替换 | 替换 | 原 `if len(term_queries)>1` 跳过 filter，单词搜索整个失效，过广 |
| D | 🔴 必须替换 | 替换 | 删除 `term_queries=[]` 初始化 → NameError 崩溃 |
| E | 🔴 必须替换 | 替换 | 原把外层 Q 加 `_connector=OR`，与"还原旧 OR 语义"等价，blast radius 大 |

四组均替换为更聚焦、blast radius 更小的高质量变异。

## 各组 Mutation 分析

### Group A — 替换（A-API 契约）
```diff
-            queryset = queryset.filter(models.Q(*term_queries))
+            queryset = queryset.filter(models.Q(*term_queries, _connector=models.Q.OR))
```
**变异语义**：保留"收集 term_queries 后单次 filter"的性能修复（JOIN 仍为 1），但把**词间**组合从默认 AND 改成 OR。于是"所有词都需匹配"退化为"任一词匹配即可"——`test_related_field_multiple_search_terms`（要求 AND 语义返回 0/1）与 `test_multiple_search_fields`（`'Mary Jonathan Duo'` 期望 0）失败，而 `test_many_search_terms`（JOIN 计数）仍通过。模拟"组合多个 Q 时默认连接符记错"的契约误解。注：本变异即原 E 的写法，但在重新分组后作为 A 的高质量代表（A 原为更粗暴的词内 AND）。

### Group B — 替换（B1 off-by-one 边界）
```diff
-            queryset = queryset.filter(models.Q(*term_queries))
+            queryset = queryset.filter(models.Q(*term_queries[:-1]))
```
**变异语义**：最终 filter 时对 `term_queries` 做 `[:-1]`，**丢掉最后一个搜索词**。典型单词或重复词场景（如 80 个相同词）影响小，但多词且最后一词具区分性时结果错误——`test_multiple_search_fields` 等多词断言失败。模拟经典的切片 off-by-one（少处理一个元素）。

### Group D — 替换（D1 状态初始化/重置）
```diff
-            term_queries = []
             for bit in smart_split(search_term):
+                term_queries = []
```
**变异语义**：把 `term_queries = []` 从循环外移到循环内，每次迭代都重置列表，最终 `term_queries` 只剩**最后一个词**的 `or_queries`。结果只按最后一词过滤，前面所有词被丢弃。模拟"初始化语句缩进错位/被挪进循环"的状态重置 bug——这是 D 组（Break State Initialization）的典型形态，且不像原 D 那样直接删除导致 NameError。

### Group E — 替换（E-测试期望/性能回退）
```diff
-            queryset = queryset.filter(models.Q(*term_queries))
+            for tq in term_queries:
+                queryset = queryset.filter(tq)
```
**变异语义**：把"单次 AND 组合 filter"还原成"逐词 `queryset.filter`"。词间 AND 语义**保持正确**（链式 filter 也是 AND），但**性能修复被撤销**——每个词在多值关系上重新产生 JOIN，`test_many_search_terms` 的 `JOIN==1` 断言失败（变多）。模拟"觉得链式 filter 更可读、把批量组合改回循环"的重构，悄悄丢失了 golden patch 的性能意图。这是与 A/B/D 正交的失败维度（性能而非结果集）。

## 新设计 Mutation 说明

四个替代分别打击不同侧面：A 改词间连接符（结果集语义，JOIN 仍对）、B 切片漏掉末词（部分词丢失）、D 把初始化挪进循环（只剩末词，状态 bug）、E 退回逐词 filter（结果对但 JOIN 性能回退）。其中 E 专门触发 JOIN 计数类断言，A/B/D 触发结果集类断言，覆盖面互补。全部通过 `py_compile`、在 `base→golden→test_patch` 后干净应用，实测均令对应 F2P 失败（rc=1）。相比原始 mutation（词内 AND 的 8 连锁失败、删除初始化的 NameError），这组替代更聚焦、更自然。
