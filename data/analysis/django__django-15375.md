# django__django-15375

## 问题背景

`annotate()` 之后再 `aggregate()` 且聚合带 `default` 参数（Django 4.0 新增）会崩溃，如 `Book.objects.annotate(idx=F("id")).aggregate(Sum("id", default=0))`。原因：带 default 的聚合会被包装成 `Coalesce(aggregate, default)`，但包装后的 `Coalesce` 没有继承原聚合的 `is_summary` 标志，导致在已有 annotation 的查询里聚合解析时归类错误而报错。Golden patch 在包装后补上 `coalesce.is_summary = c.is_summary`。

## Golden Patch 语义分析

```python
c.default = None  # Reset the default argument before wrapping.
coalesce = Coalesce(c, default, output_field=c._output_field_or_none)
coalesce.is_summary = c.is_summary
return coalesce
```
核心语义：**包装出的 `Coalesce` 必须继承被包装聚合 `c` 的 `is_summary` 标志**。`is_summary` 标记该表达式是否为"汇总"（aggregate-over-query）级别；annotate 之后做 aggregate 时，SQL 编译会依据 `is_summary` 决定该表达式归入 GROUP BY 还是作为汇总列。缺失或错误的 `is_summary` 会让 Coalesce 被误归类，引发 `annotate().aggregate(default=...)` 崩溃。

F2P 测试：`AggregateTestCase.test_aggregation_default_after_annotation`（annotate 后 aggregate 带 default，期望正确结果 40）、`test_aggregation_default_not_in_aggregate`（annotate 里带 default 的 Avg，aggregate 另一字段，期望 20）。

## 调用链分析

`Aggregate.resolve_expression` 在 `default` 非 None 时把自身 `c` 包成 `Coalesce`，必须把 `c.is_summary` 复制给 `coalesce`。下游 SQL compiler 读取 `is_summary` 决定分组/汇总归属。该修复点是**单独一行赋值**，因此让 F2P 失败的唯一途径就是让 `coalesce.is_summary` 不等于 `c.is_summary`（值错、对象错、或根本没设）。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原 A=C（都删整行）；改为"从 self 取值"（错误来源对象） |
| B | 🟡 | 替换 | 原 B=D（都注释掉）；改为"硬编码 False" |
| C | 🔴 必须替换 | 替换 | 与 A 重复（删行）；保留"删行"机制作为 C |
| D | 🔴 必须替换 | 替换 | 与 B 重复（注释）；保留"注释"机制作为 D |
| E | ➕ 补充 | 新增 | 原缺 E 组 |

原四组只有两种（删行 ×2 + 注释 ×2）。由于修复仅一行，所有变异都必须破坏该赋值，但通过不同机制（错来源 / 错值 / 删除 / 注释 / 错目标）实现多样化，并补齐缺失的 E。

## 各组 Mutation 分析

### Group A — 替换（A1 接口契约：错误来源对象）
**原**：删整行（与 C 重复）。
**最终 mutation**：
```diff
-        coalesce.is_summary = c.is_summary
+        coalesce.is_summary = self.is_summary
```
**变异语义**：从 `self.is_summary` 取值而非 `c.is_summary`。`c = self.copy()` 后经过 resolve/summarize 处理，`c.is_summary` 才是正确的汇总标志；`self`（未解析的原始聚合）的 `is_summary` 可能不同（如 False）。于是 coalesce 拿到错误的标志，annotate 后 aggregate 误归类。模拟"copy 后该用副本却用了原对象"的来源混淆——极隐蔽，因 `self`/`c` 都有该属性、不报错。

### Group B — 替换（B2 边界/硬编码值）
**原**：注释掉（与 D 重复）。
**最终 mutation**：
```diff
-        coalesce.is_summary = c.is_summary
+        coalesce.is_summary = False
```
**变异语义**：把 `is_summary` 硬编码为 `False`。当 `c.is_summary` 本应为 True（aggregate 顶层汇总）时，被错误地置 False，coalesce 被当作非汇总表达式，annotate 后 aggregate 崩溃。模拟"以为这里一定是非汇总、直接写死 False"的边界假设。

### Group C — 替换/保留机制（C-数据形状：属性从未设置）
```diff
        coalesce = Coalesce(c, default, output_field=c._output_field_or_none)
-        coalesce.is_summary = c.is_summary
        return coalesce
```
**变异语义**：删掉赋值行，`coalesce.is_summary` 退回 `Coalesce`/`Expression` 类默认值（通常 False），与 `c.is_summary` 不符。原 A/C 即此写法，保留为 C。属性形状缺失导致的归类错误。

### Group D — 替换/保留机制（D1 状态初始化未完成）
```diff
-        coalesce.is_summary = c.is_summary
+        # coalesce.is_summary = c.is_summary
```
**变异语义**：注释掉赋值行，效果同 C（属性未被初始化为正确值），但形式是"被注释的死代码"。原 B/D 即此写法，保留为 D。模拟"临时注释掉调试、忘了恢复"。

### Group E — 补充（E1 测试期望：写到错误目标）
```diff
-        coalesce.is_summary = c.is_summary
+        c.is_summary = c.is_summary
```
**变异语义**：把赋值目标从 `coalesce` 写成 `c`（`c.is_summary = c.is_summary` 是无操作自赋值）。真正返回的 `coalesce` 的 `is_summary` 从未被设置，保持默认值，归类错误。模拟"赋值左侧对象名写错"——看起来在设置 is_summary，实则设到了不会被用的对象上。与 C/D（删/注释）不同，这里有一行"像在干活"的代码，更具迷惑性。

## 新设计 Mutation 说明

修复仅 `coalesce.is_summary = c.is_summary` 一行，原四组只有"删行"和"注释"两种重复写法。本次保留这两种作为 C、D，并补充三种破坏同一赋值的新机制：A（从 `self` 取错误来源值）、B（硬编码 False）、E（写到错误目标 `c`，自赋值无效）。补齐缺失的 E。五组覆盖"错来源对象 / 错值 / 删除 / 注释 / 错目标对象"五个角度，其中 A、E 因"看似在赋值"而最隐蔽。全部实测：golden 通过、变异令两个 F2P 测试失败、`base→golden→test_patch` 后干净应用。
