# django__django-11951

## 问题背景

`QuerySet.bulk_create()` 方法在用户提供了 `batch_size` 参数时，会直接使用该值作为批量插入的大小，而不检查它是否超过了数据库后端所能支持的最大批量大小（由 `DatabaseOperations.bulk_batch_size()` 返回）。这与同文件中的 `bulk_update()` 方法行为不一致——`bulk_update()` 已正确使用 `min(batch_size, max_batch_size)` 来取较小值。

在 SQLite 等有参数数量限制的数据库后端，若用户提供的 `batch_size` 超过数据库最大值，就会导致每批参数数量超过数据库限制（SQLite 默认 999 个变量），从而引发运行时错误。

**Golden patch 修复**：在 `_batched_insert()` 方法中，从原来的 `batch_size = (batch_size or max(ops.bulk_batch_size(fields, objs), 1))` 改为先计算 `max_batch_size`，再用 `min(batch_size, max_batch_size)` 取较小值，逻辑与 `bulk_update()` 保持一致。

## Golden Patch 语义分析

修复的核心逻辑：
- **修复前**：`batch_size = (batch_size or max(ops.bulk_batch_size(fields, objs), 1))` — 若用户提供了 `batch_size`（非 None 非 0），则直接使用，完全忽略数据库后端的限制。
- **修复后**：先计算 `max_batch_size = max(ops.bulk_batch_size(fields, objs), 1)` 作为数据库能接受的上界，再用 `min(batch_size, max_batch_size)` 确保用户提供的值不超过该上界。

修复的"为什么"：用户提供的 `batch_size` 表示"期望的批大小"，是一个上限提示；数据库后端计算的 `max_batch_size` 是硬性约束。两者取最小才是正确语义。这与 `bulk_update()` 中已有的处理方式完全一致，统一了 API 行为。

## 调用链分析

```
QuerySet.bulk_create(objs, batch_size=None, ignore_conflicts=False)
    └── QuerySet._batched_insert(objs, fields, batch_size, ignore_conflicts=False)  ← golden patch 修改处
            └── DatabaseOperations.bulk_batch_size(fields, objs)   ← 返回数据库后端的最大批大小
                    SQLite: 返回 999 // len(fields)（当 len(fields) > 1 时）
                    Base:   返回 len(objs)（无限制）
            └── QuerySet._insert(item, fields, using, returning_fields, ignore_conflicts)
```

`bulk_update()` 调用链（对比参考）：
```
QuerySet.bulk_update(objs, fields, batch_size=None)
    └── 直接在方法体内计算 max_batch_size 并 min(batch_size, max_batch_size)
    └── 不经过 _batched_insert
```

数据流：`bulk_create` 将对象拆分为 `objs_with_pk` 和 `objs_without_pk` 两组，分别调用 `_batched_insert`，传入不同的 `fields`（后者去掉了 AutoField）。`_batched_insert` 负责将对象按批分组，每批调用 `_insert`。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 跨函数语义变异（A1） | 新设计 | 在 bulk_batch_size 调用中错误地添加 'pk'，模拟开发者看到 bulk_update 代码后的误推广 |
| B | 逻辑反转变异（B3） | 新设计 | 将 min 改为 max，模拟 min/max 混淆错误 |
| C | 类型隐式强制变异（C1） | 新设计 | 只取第一个字段传入 bulk_batch_size，导致使用了错误的后端限制 |
| D | 顺序依赖变异（D3） | 新设计 | 在 max_batch_size 约束应用前就设定了 batch_size，引入顺序依赖 bug |
| E | 断言预期更新变异（E1） | 新设计 | 将静默截断改为抛出 ValueError，使依赖静默行为的测试失败 |

Path B 流程：mutations.jsonl 中仅有 1 条（Group D），不足 5 条，跳过 Step 2，直接为 A/B/C/D/E 各设计一个全新高质量 mutation。

## 各组 Mutation 分析

### Group A — A1（新设计）

**原 mutation**：无（Path B，全新设计）

**最终 mutation**：
```diff
diff --git a/django/db/models/query.py b/django/db/models/query.py
index 92349cd0c5..368988eecc 100644
--- a/django/db/models/query.py
+++ b/django/db/models/query.py
@@ -1209,7 +1209,7 @@ class QuerySet:
         if ignore_conflicts and not connections[self.db].features.supports_ignore_conflicts:
             raise NotSupportedError('This database backend does not support ignoring conflicts.')
         ops = connections[self.db].ops
-        max_batch_size = max(ops.bulk_batch_size(fields, objs), 1)
+        max_batch_size = max(ops.bulk_batch_size(['pk'] + list(fields), objs), 1)
         batch_size = min(batch_size, max_batch_size) if batch_size else max_batch_size
         inserted_rows = []
         bulk_return = connections[self.db].features.can_return_rows_from_bulk_insert
```

**变异语义**：开发者看到 `bulk_update` 在调用 `bulk_batch_size(['pk', 'pk'] + fields, ...)` 时包含了 PK 字段，可能错误地推广到 `_batched_insert`，也加入了 `'pk'`。实际上 `bulk_update` 需要包含 PK 因为每次更新的 WHERE 和 CASE 都需要引用 PK 参数，但 `bulk_create` 的插入 SQL 不含 PK 参数（AutoField 被过滤掉了）。
- **为什么难以发现**：代码变化只有一个字符串字面量 `['pk'] +`，看起来是参考了相邻的 `bulk_update` 模式，显得合乎情理。
- **SQLite 上**：3 字段 → `999//4 = 249`（而非 `999//3 = 333`），`min(334, 249) = 249`，实际查询数 `ceil(1000/249) = 5`（期望 4）→ F2P 失败 ✓
- **通过的测试**：不指定 `batch_size` 时行为基本正确（`max_batch_size` 变小，需要更多查询，但测试通常不检查确切查询数）；指定合理小 `batch_size`（如 10）时不受影响。

### Group B — B3（新设计）

**最终 mutation**：
```diff
diff --git a/django/db/models/query.py b/django/db/models/query.py
index 92349cd0c5..1e2bb62a6a 100644
--- a/django/db/models/query.py
+++ b/django/db/models/query.py
@@ -1210,7 +1210,7 @@ class QuerySet:
             raise NotSupportedError('This database backend does not support ignoring conflicts.')
         ops = connections[self.db].ops
         max_batch_size = max(ops.bulk_batch_size(fields, objs), 1)
-        batch_size = min(batch_size, max_batch_size) if batch_size else max_batch_size
+        batch_size = max(batch_size, max_batch_size) if batch_size else max_batch_size
         inserted_rows = []
         bulk_return = connections[self.db].features.can_return_rows_from_bulk_insert
         for item in [objs[i:i + batch_size] for i in range(0, len(objs), batch_size)]:
```

**变异语义**：将 `min` 改为 `max`，完全反转了约束逻辑。现在 `batch_size` 会取用户提供值与数据库最大值中的**较大者**——当用户提供的值超过数据库限制时，反而会使用更大的值，恢复了原始 bug 的行为。
- **为什么难以发现**：`min` vs `max` 的拼写几乎相同，代码审查时极容易视觉忽略；在用户提供的 `batch_size` 小于 `max_batch_size` 的常见场景下行为完全正确（`max(小, 大) = 大 = max_batch_size`，退化为默认值）。
- **SQLite 上**：`max(334, 333) = 334`，`ceil(1000/334) = 3`（期望 4）→ F2P 失败 ✓
- **通过的测试**：`test_explicit_batch_size_efficiency`（用 `batch_size=50` 和 `batch_size=len(objs)`，两者均 ≤ max_batch_size，`max(50, 333)=333` 则变为 2 次查询... 实际上这个测试也会失败）。

### Group C — C1（新设计）

**最终 mutation**：
```diff
diff --git a/django/db/models/query.py b/django/db/models/query.py
index 92349cd0c5..5ef56197ac 100644
--- a/django/db/models/query.py
+++ b/django/db/models/query.py
@@ -1209,7 +1209,7 @@ class QuerySet:
         if ignore_conflicts and not connections[self.db].features.supports_ignore_conflicts:
             raise NotSupportedError('This database backend does not support ignoring conflicts.')
         ops = connections[self.db].ops
-        max_batch_size = max(ops.bulk_batch_size(fields, objs), 1)
+        max_batch_size = max(ops.bulk_batch_size(list(fields)[:1], objs), 1)
         batch_size = min(batch_size, max_batch_size) if batch_size else max_batch_size
         inserted_rows = []
         bulk_return = connections[self.db].features.can_return_rows_from_bulk_insert
```

**变异语义**：仅用第一个字段来计算后端允许的最大批大小，而非全部字段。开发者可能误认为"批大小取决于字段数中最受限的那一个（第一个），而不是总字段数"。
- **为什么难以发现**：`list(fields)[:1]` 看起来像是某种优化或"保守估计"的思路；代码在字段数为 1 时行为完全正确。
- **SQLite 上**：1 字段时返回 500（而非 3 字段的 333），`max_batch_size = 500`，`min(334, 500) = 334`，`ceil(1000/334) = 3`（期望 4）→ F2P 失败 ✓
- **通过的测试**：当字段数为 1（如只插入单字段模型）时，`fields[:1] = fields`，行为完全正确。

### Group D — D3（新设计）

**最终 mutation**：
```diff
diff --git a/django/db/models/query.py b/django/db/models/query.py
index 92349cd0c5..4aad004960 100644
--- a/django/db/models/query.py
+++ b/django/db/models/query.py
@@ -1209,8 +1209,8 @@ class QuerySet:
         if ignore_conflicts and not connections[self.db].features.supports_ignore_conflicts:
             raise NotSupportedError('This database backend does not support ignoring conflicts.')
         ops = connections[self.db].ops
+        batch_size = batch_size or max(ops.bulk_batch_size(fields, objs), 1)
         max_batch_size = max(ops.bulk_batch_size(fields, objs), 1)
-        batch_size = min(batch_size, max_batch_size) if batch_size else max_batch_size
         inserted_rows = []
         bulk_return = connections[self.db].features.can_return_rows_from_bulk_insert
         for item in [objs[i:i + batch_size] for i in range(0, len(objs), batch_size)]:
```

**变异语义**：引入顺序依赖错误——在 `max_batch_size` 计算之前就设定了 `batch_size`（使用旧逻辑 `batch_size or fallback`），然后计算了 `max_batch_size` 但不再使用它来约束 `batch_size`。形式上两个计算都存在，但顺序颠倒导致约束失效。
- **为什么难以发现**：两行都保留，`max_batch_size` 也被计算出来（只是没有被使用），代码阅读时不容易发现 `max_batch_size` 已成为死代码；这种"计算了但没用上"的模式在重构时很常见。
- **SQLite 上**：`batch_size = 334 or ...` = `334`（truthy），后续 `max_batch_size = 333` 未用，`ceil(1000/334) = 3`（期望 4）→ F2P 失败 ✓
- **通过的测试**：`batch_size=None` 时，`None or max(333, 1) = 333`，行为正确。

### Group E — E1（新设计）

**最终 mutation**：
```diff
diff --git a/django/db/models/query.py b/django/db/models/query.py
index 92349cd0c5..01dca778ef 100644
--- a/django/db/models/query.py
+++ b/django/db/models/query.py
@@ -1210,7 +1210,12 @@ class QuerySet:
             raise NotSupportedError('This database backend does not support ignoring conflicts.')
         ops = connections[self.db].ops
         max_batch_size = max(ops.bulk_batch_size(fields, objs), 1)
-        batch_size = min(batch_size, max_batch_size) if batch_size else max_batch_size
+        if batch_size and batch_size > max_batch_size:
+            raise ValueError(
+                'Provided batch_size %d exceeds the maximum batch size %d supported '
+                'by this database backend.' % (batch_size, max_batch_size)
+            )
+        batch_size = batch_size or max_batch_size
         inserted_rows = []
         bulk_return = connections[self.db].features.can_return_rows_from_bulk_insert
         for item in [objs[i:i + batch_size] for i in range(0, len(objs), batch_size)]:
```

**变异语义**：将"静默截断 batch_size 至 max_batch_size"改为"当 batch_size 超过限制时抛出 ValueError"。从 API 设计角度这是合理的，但与测试的行为预期不符——F2P 测试的核心就是验证"超出限制的 batch_size 应被静默截断而不是报错"。
- **为什么难以发现**：抛出 ValueError 是一种合理的防御性编程风格，代码读起来合情合理；错误消息也很专业。很多 API 确实采用"参数超出范围就报错"而非"静默截断"的策略。
- **SQLite 上**：`batch_size=334 > max_batch_size=333`，抛出 `ValueError`，`assertNumQueries` 上下文管理器捕获到异常而非查询计数不符 → F2P 失败 ✓
- **通过的测试**：`batch_size=None` 或 `batch_size ≤ max_batch_size` 的场景完全正常，不会触发 ValueError。

## 新设计 Mutation 说明

### 设计思路（所有 5 个）

**代码分析基础**：
1. Golden patch 修复的核心是在 `_batched_insert` 中增加了 `max_batch_size = max(ops.bulk_batch_size(fields, objs), 1)` 计算，并将原来的 `(batch_size or fallback)` 改为 `min(batch_size, max_batch_size) if batch_size else max_batch_size`。
2. `bulk_update()` 方法在修复前就已经有正确的 `min()` 逻辑，是参考对象。
3. SQLite 的 `bulk_batch_size` 根据字段数计算：1 字段返回 500，多字段返回 `999 // len(fields)`。
4. F2P 测试使用 3 字段 Country 模型，1000 个对象，`batch_size=max_batch_size+1=334`，期望 `ceil(1000/333)=4` 次查询。

**为什么这 5 个 mutation 是高质量的**：
- **A（A1）**：模拟开发者误读 `bulk_update` 代码（该方法传入 `['pk', 'pk'] + fields`）并错误地将这个模式应用到 `bulk_create`。这是真实的"类比迁移错误"。
- **B（B3）**：min/max 混淆是统计上最常见的一类逻辑错误，且在 `batch_size < max_batch_size` 的绝大多数情况下行为正确，只在边界值下暴露。
- **C（C1）**：对 `fields` 做了看似无害的切片 `[:1]`，可能来自对"保守估计"或"最坏情况字段"的误解。只在多字段模型上暴露，单字段模型行为正常。
- **D（D3）**：引入顺序依赖——两行计算都保留，只是顺序改变导致约束失效，`max_batch_size` 成为死代码。重构时容易引入这类错误。
- **E（E1）**：改变了 API 的错误处理策略（从静默截断到抛出异常），这是设计层面的分歧，而非明显的编码错误，代码审查时难以判断对错。
