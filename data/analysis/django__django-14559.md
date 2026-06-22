# django__django-14559 变异分析

## 问题背景
`QuerySet.bulk_update()` 此前返回 `None`，与 `update()`（返回匹配行数）不一致。Issue 要求让 `bulk_update()` 也返回匹配的行数：因为 `bulk_update()` 内部就是反复调用 `update()`，把每个 batch 的返回值累加即可。

## Golden Patch 语义分析
golden patch 在 `django/db/models/query.py` 的 `bulk_update()` 中做了三处改动：
1. 空对象快速返回路径 `return` → `return 0`（保证返回 int 而非 None）。
2. 进入更新循环前新增累加器 `rows_updated = 0`。
3. 循环内 `self.filter(pk__in=pks).update(**update_kwargs)` → `rows_updated += self.filter(...).update(...)`，循环结束后 `return rows_updated`。

语义关键点：
- 返回值必须是 **数据库真实匹配的行数**（来自 `update()`），而非传入对象/pk 的个数。
- 当传入重复对象时，多个 pk 落在同一行，匹配行数 < 传入对象数；但若分到不同 batch，则会被重复统计（这是被测行为）。
- 累加器初始化必须在循环外。

## 调用链分析
`bulk_update()` → 按 `batch_size` 切分 `updates` 列表 → `transaction.atomic` 内逐 batch 调用 `self.filter(pk__in=pks).update(**update_kwargs)` → `update()` 返回该 batch 的匹配行数 → 累加 → 返回。

## F2P 测试
- `test_empty_objects`：`bulk_update([], ['note'])` 应返回 `0`。
- `test_large_batch`：2000 个对象（会跨多个 batch）应返回 `2000`。
- `test_updated_rows_when_passing_duplicates`：传 `[note, note]` 同一 batch 应返回 `1`（仅 1 行匹配）；`batch_size=1` 分到两个 batch 应返回 `2`。

## 替换决策总览

| 组 | 类别 | 决策 | 原因 |
|----|------|------|------|
| A | 🟡 SEMANTIC-SHALLOW | 保留 | 单 token `+=`→`=`，自然的累加 bug，失败模式为 last-batch-only |
| B | 🟡 SEMANTIC-SHALLOW | 替换 | `return x-1` 任意常数偏移，是 shallow 对中较弱者（floor(2/2)=1），改为只破坏空路径 |
| C | 🔴 MUST REPLACE | 替换 | `rows_updated="0"` 触发 TypeError，导致 19 个测试（含 P2P）报错崩溃，非自然、不隐蔽 |
| D | 🟢 KEEP | 保留 | 多行、把初始化误置于循环内，机制独特（D1），自然的开发者失误 |
| E | 🔴 MUST REPLACE | 替换 | 新增 `return_count=False` 并让默认仍返回 None，本质是 golden 行为的功能等价回退 |

## 各组 Mutation 分析

### Slot A —— 保留（B1）
原始 diff：循环内 `rows_updated += ...` 改为 `rows_updated = ...`。
- 分类：🟡 单 token 但失败模式有价值。
- 验证：失败 `test_large_batch` + `test_updated_rows_when_passing_duplicates`（仅统计最后一个 batch）。空路径与单 batch 仍通过，故隐蔽。
- 变异语义：累加退化为赋值，只保留最后一个 batch 的计数（B1 off-by-... last-only）。

### Slot B —— 替换（B2）
- 原始 diff：`return rows_updated - 1`。任意减一，且失败模式与 A 重叠（large_batch+duplicates），属较弱 shallow。
- 决策：替换。
- 新 diff：把空对象守卫 `return 0` 改回 `return`（None）。
- 变异语义：移除空集合的显式 0 返回处理（B2 去除空/边界处理）。
- 验证：**仅** `test_empty_objects` 失败，其余全过 —— 与 A/C/D/E 失败模式完全正交。

### Slot C —— 替换（C1）
- 原始 diff：`rows_updated = "0"`，str 与 int 相加抛 TypeError，19 个测试（含 P2P）崩溃 —— 不隐蔽、非自然伪影。
- 决策：替换。
- 新 diff：循环内改为先执行 `update()`（保留副作用与查询计数），再 `rows_updated += len(pks)`，用传入 pk 数代替数据库真实匹配行数。
- 变异语义：用"请求的 pk 数量"近似"实际匹配行数"（C1 类型/数据形状误用）。
- 验证：**仅** `test_updated_rows_when_passing_duplicates` 失败 —— 同 batch 内重复 pk 时 `len(pks)=2` 但实际匹配 1 行。其它（含 large_batch 因 pk 唯一、empty 因不进循环）全过，高度隐蔽。

### Slot D —— 保留（D1）
原始 diff：删除循环外 `rows_updated = 0`，改置于 `for` 循环体首行。
- 分类：🟢 多行、状态初始化时机错误，机制独立。
- 验证：失败 `test_large_batch` + `test_updated_rows_when_passing_duplicates`（每轮被清零，仅留最后 batch）。
- 变异语义：累加器初始化被错误地搬入循环内（D1 状态初始化/重置）。

### Slot E —— 替换（C1）
- 原始 diff：新增 `*, batch_size=None, return_count=False` 参数，默认仍返回 None。这是把 golden 修复"默认返回行数"退回原始"默认返回 None"的功能等价回退，且引入了未被使用的伪配置参数（非自然伪影）。
- 决策：替换。
- 新 diff：`return rows_updated` 改为 `return len(updates)`，返回 batch 个数而非累加行数。
- 变异语义：用 batch 数量代替总行数返回（C1 数据形状混淆）。
- 验证：**仅** `test_large_batch` 失败 —— 2000 行单 batch 时返回 1≠2000；empty（0 batch 走快速返回）、单行、duplicates（1 batch=1 行恰好巧合）均通过，难以察觉。

## 新设计 Mutation 说明

- **B（B2）**：基于 golden patch 第一处改动（空守卫 `return`→`return 0`）。该位置专管空输入返回值，模拟"开发者忘了让空路径也返回 0"的失误。难检测：只有显式断言 `bulk_update([], ...) == 0` 的测试才会暴露。验证：仅 `test_empty_objects` 失败。
- **C（C1）**：基于循环内累加来源。模拟"误用传入 pk 数量当作匹配行数"的常见认知错误（开发者常以为返回的是处理对象数）。难检测：仅在重复 pk 落入同一 batch、匹配行数 < pk 数时才偏差；普通唯一-pk 场景全部巧合通过。验证：仅 `test_updated_rows_when_passing_duplicates` 失败。
- **E（C1）**：基于最终 `return rows_updated`。模拟"误返回 batch 计数而非行数"的形状混淆。难检测：单 batch / 空集合 / 1 行场景下 batch 数与行数恰好相等或都走快速返回，只有大批量跨 batch 才暴露。验证：仅 `test_large_batch` 失败。

## 失败模式正交性总结
- A：large_batch + duplicates（last-batch-only，累加退化）
- B：empty_objects（空路径）
- C：duplicates（pk 数 vs 行数）
- D：large_batch + duplicates（循环内重置，机制不同于 A）
- E：large_batch（batch 数 vs 行数）

三个 F2P 断言点（0 / 2000 / 1&2）被不同 mutation 分别覆盖，最大化了失败模式多样性。
