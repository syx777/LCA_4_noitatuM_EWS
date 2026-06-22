# django__django-14170

## 问题背景

`YearLookup` 的查询优化用 `BETWEEN <下界> AND <上界>` 替代 `EXTRACT` 操作，使索引可用。但该优化错误地也应用于 `__iso_year`（`ExtractIsoYear`）查询。ISO-8601 周编号年与日历年并不一致（ISO 年可以从上一日历年的 12 月底开始、延伸到下一年初），因此用日历年的 `[Jan 1, Dec 31]` 作为 `BETWEEN` 边界会返回错误数据。

表现：`ExtractIsoYear` 作为 annotation 单独使用正确，但一旦用于 `filter(start_date__iso_year=2020)` 就触发 BETWEEN 优化，产生错误结果。

## Golden Patch 语义分析

修改两处文件：

1. `django/db/backends/base/operations.py`：`year_lookup_bounds_for_date_field` 和 `year_lookup_bounds_for_datetime_field` 新增 `iso_year=False` 参数。当 `iso_year=True` 时，用 `datetime.date.fromisocalendar(value, 1, 1)`（ISO 第 1 周周一）作为下界，用 `fromisocalendar(value + 1, 1, 1) - timedelta(...)`（下一个 ISO 年首日的前一刻）作为上界；否则保持日历年 `[Jan 1, Dec 31]`。

2. `django/db/models/lookups.py`：`YearLookup.year_lookup_bounds` 通过 `iso_year = isinstance(self.lhs, ExtractIsoYear)` 检测是否为 ISO 年查询，并把该标志传递给上面两个 bounds 方法。

语义核心：**检测 ISO 年（lookups.py）** + **正确计算 ISO 边界（operations.py）** 两段协作，缺一不可。

## 调用链分析

`filter(field__iso_year=N)` → `YearExact/YearGt/...`（继承 `YearLookup`）→ `as_sql`（rhs 为直接值时走优化分支）→ `year_lookup_bounds(connection, self.rhs)` → 根据 `output_field` 类型调用 `connection.ops.year_lookup_bounds_for_datetime_field(year, iso_year=...)` 或 `..._for_date_field(...)` → 返回 `[start, finish]` → `get_bound_params` 取边界生成 `BETWEEN` 参数。

F2P 测试 `test_extract_iso_year_func_boundaries`（`tests/db_functions/datetime/test_extract_trunc.py`）构造三条横跨 ISO 年边界的日期（2014-12-27、2014-12-31、2015-12-31），断言 `start_datetime__iso_year=2015`、`__iso_year__gt=2014`、`__iso_year__lte=2014` 返回正确对象集合。该测试只走 DateTimeField 路径。

P2P 测试 `test_extract_year_exact/greaterthan/lessthan_lookup` 用 `for lookup in ('year','iso_year')` 子测试，但其数据为年中（6 月）日期，ISO 年与日历年一致，因此对 ISO 边界细节不敏感。

## 替换决策总览

| 组 | 原类别 | 决策 | 原因 |
|----|--------|------|------|
| A | 🔴 不自然产物 | 替换 (C2) | `iso_year = False  # Bug` 含 "Bug" 注释，且直接回退 golden（关掉修复） |
| B | 🟡 浅层单 token | 保留 (C1) | `not isinstance(...)` 取反，建模真实条件反转错误，落在关键控制流节点 |
| C | 🔴 功能等价冗余 | 替换 (C2) | 计算出 `iso_year` 后又删除 `iso_year=iso_year` 传参，等价于关掉修复 |
| D | 🔴 不自然产物 | 替换 (A2) | `iso_year = False  # BUG`，与 A 重复且含 BUG 注释 |
| E | 🔴 功能等价冗余/死守卫 | 替换 (A2) | 新增 `detect_iso_year=False` 守卫，调用方从不传 True，永远 False |

M（浅层 🟡）= 1（仅 B）。但 A/C/D/E 四者行为完全坍缩为同一后果（ISO 边界从不生效），构成严重冗余 + 不自然产物，全部替换；保留 B 作为唯一保留的浅层条件变异。所有替换设计为正交失败模式，分布在 ISO 边界计算的不同位置（下界/上界、日期构造方式、weekday/week 索引）。

## 各组 Mutation 分析

### A 组
- 原 diff：`iso_year = isinstance(self.lhs, ExtractIsoYear)` → `iso_year = False  # Bug: always False`
- 分类：🔴 不自然产物（"Bug" 注释）+ 直接 golden 反向
- 理由：注释暴露意图，且把 lookups.py 检测直接关死，等于把整段修复回退。
- 最终 diff（operations.py，datetime ISO 下界）：
```
-            first = datetime.datetime.fromisocalendar(value, 1, 1)
+            first = datetime.datetime(value, 1, 1)
```
- 变异语义：ISO 年下界误用日历 `datetime(value,1,1)` 而非 ISO 第 1 周周一。多数年份两者相同，年中过滤与日历年查询不受影响；只有跨 ISO 周边界（年初几天）的记录会被错误排除。

### B 组（保留）
- 原 diff：`iso_year = not isinstance(self.lhs, ExtractIsoYear)`
- 分类：🟡 浅层单 token（`not`）
- 理由：建模真实"条件取反"错误，位于关键控制流节点（ISO 检测）。其后果只在 ISO 年≠日历年时显现，年中数据全部通过，看起来像一个合理的守卫，难以被泛化测试发现。保留为唯一浅层变异。
- 最终 diff：见上文（lookups.py，`iso_year = not isinstance(...)`）。
- 变异语义：日历年查询拿到 ISO 边界、ISO 查询拿到日历边界，互换错误。

### C 组
- 原 diff：`type(self.lhs) is ExtractIsoYear` + 删除两处 `iso_year=iso_year` 传参
- 分类：🔴 功能等价冗余（算出标志却不传递 = 关掉修复）
- 理由：与 A/D/E 后果相同，且"算了又丢弃"是明显人造痕迹。
- 最终 diff（operations.py，datetime ISO 上界）：
```
-            second = (
-                datetime.datetime.fromisocalendar(value + 1, 1, 1) -
-                datetime.timedelta(microseconds=1)
-            )
+            second = datetime.datetime(value, 12, 31, 23, 59, 59, 999999)
```
- 变异语义：ISO 年上界误用日历 12-31 末刻。ISO 年可延伸到次年 1 月初，被截断后会漏掉如 2015-12-31 这类属于更长 ISO 年的记录。

### D 组
- 原 diff：`iso_year = False  # BUG: removed ExtractIsoYear check`
- 分类：🔴 不自然产物，且与 A 完全重复
- 理由：BUG 注释 + 与 A 同一改动，纯冗余。
- 最终 diff（operations.py，datetime ISO 下界 weekday 索引）：
```
-            first = datetime.datetime.fromisocalendar(value, 1, 1)
+            first = datetime.datetime.fromisocalendar(value, 1, 7)
```
- 变异语义：ISO 下界 weekday 取 7（ISO 第 1 周周日）而非 1（周一），起点后移 6 天，排除 ISO 年起始数日内的边界记录。

### E 组
- 原 diff：新增 `detect_iso_year=False` 参数，`iso_year = isinstance(...) if detect_iso_year else False`
- 分类：🔴 死守卫/功能等价冗余（调用方从不传 True）
- 理由：新增的开关默认关闭且无人开启，等于永久禁用修复，是典型"加守卫禁用 fix"产物。
- 最终 diff（operations.py，datetime ISO 上界 week 索引）：
```
-                datetime.datetime.fromisocalendar(value + 1, 1, 1) -
+                datetime.datetime.fromisocalendar(value + 1, 2, 1) -
```
- 变异语义：上界 ISO 周索引取 2 而非 1，上边界多延伸一周，使下一 ISO 年第 1 周的记录漏入当前年 BETWEEN 区间。

## 新设计 Mutation 说明

四个替换（A、C、D、E）全部落在 `year_lookup_bounds_for_datetime_field` 的 ISO 分支（F2P 仅走 DateTimeField），保留 golden 的检测与传参链路完整，因此：
- 日历年查询（非 ISO）完全不受影响；
- 年中 ISO 查询（`year` 与 `iso_year` 一致的数据）通过；
- 仅在 ISO 周边界日期（测试数据 2014-12-27 / 2014-12-31 / 2015-12-31）暴露错误。

四者攻击不同维度，互相正交：
- A：下界 — 日期构造方式（ISO vs 日历）
- C：上界 — 日期构造方式（ISO vs 日历）
- D：下界 — ISO weekday 索引（1 vs 7）
- E：上界 — ISO week 索引（1 vs 2）
B 保留在 lookups.py 的检测节点，构成第五个正交维度（条件取反）。

## 验证结果（REAL）

- 基线（golden + test_patch，无变异）：`db_functions.datetime.test_extract_trunc` 81 tests，OK (skipped=2) — PASS。
- 模块路径：`db_functions.datetime.test_extract_trunc`；运行命令 `python3 tests/runtests.py db_functions.datetime.test_extract_trunc --parallel 1 -v 1`，`PYTHONPATH=<tmp>`。
- A / B / C / D / E：均 `git apply` 成功、`py_compile` OK；模块运行 FAILED (failures=2)，且失败仅为 `test_extract_iso_year_func_boundaries`（`DateFunctionTests` 与 `DateFunctionWithTimeZoneTests` 两个变体），无 P2P 回归。
- 结论：5 个最终变异全部满足"applies + compiles + 仅 F2P 失败"。
