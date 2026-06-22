# django__django-15554

## 问题背景

对同一关系使用多个不同 filter 的 `FilteredRelation`（如 `book_title_alice` 与 `book_title_jane` 都基于 `book` 关系但 condition 不同）时，第二个会被忽略——它们错误地复用了同一个 join alias。Golden patch 给 `join`/`build_filter`/`setup_joins` 增加 `reuse_with_filtered_relation` 参数，并在 filtered-relation 复用分支用 `j.equals(join)`（忽略 `filtered_relation` 的弱比较）而非 `j == join`（强比较，含 filtered_relation），使不同 filter 的同关系 join 能区分。

## Golden Patch 语义分析

```python
def join(self, join, reuse=None, reuse_with_filtered_relation=False):
    ...
    if reuse_with_filtered_relation and reuse:
        reuse_aliases = [
            a for a, j in self.alias_map.items() if a in reuse and j.equals(join)
        ]
    else:
        reuse_aliases = [
            a for a, j in self.alias_map.items()
            if (reuse is None or a in reuse) and j == join
        ]
```
核心语义：**计算 FilteredRelation 时走专门分支，仅在 `can_reuse` 集合内、用 `j.equals(join)` 弱比较来判定可复用 alias**。`Join.equals` 比较时忽略 `filtered_relation`，`__eq__` 则把它计入。配合 `setup_joins` 里 `reuse = can_reuse if join.m2m or reuse_with_filtered_relation else None` 与 `build_filtered_relation_q` 传 `reuse_with_filtered_relation=True`，多个 FilteredRelation 才能各自建立 join。

F2P 测试 `FilteredRelationTests.test_multiple`：两个不同 condition 的 FilteredRelation 基于同一 `book` 关系，断言各自正确取到对应过滤结果。

## 调用链分析

`build_filtered_relation_q`（传 `reuse_with_filtered_relation=True`）→ `build_filter` → `setup_joins`（计算 `reuse`、传递标志）→ `join`（按标志选比较方式）。三处协同：标志传递、reuse 计算、alias 比较方式。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原 A=B=C 三者重复（`j.equals`→`j == join`）；改为 `a in reuse or j.equals(join)` |
| B | 🟡 | 替换 | 与 A/C 重复；保留"强比较 `j == join`"机制作为 B |
| C | 🔴 必须替换 | 替换 | 与 A/B 重复；改为 `j is join` 身份比较 |
| D | 🟢 高质量 | 替换 | 原 D 把双分支合并成单分支用 `j.equals`；改为 build_filtered_relation_q 传 `reuse_with_filtered_relation=False` |
| E | 🟢 高质量 | 保留 | 交换两分支的比较运算（equals↔==） |

原 A=B=C 三者完全相同，重做使五组机制各异。

## 各组 Mutation 分析

### Group A — 替换（B3 布尔逻辑：and→or）
```diff
-                a for a, j in self.alias_map.items() if a in reuse and j.equals(join)
+                a for a, j in self.alias_map.items() if a in reuse or j.equals(join)
```
**变异语义**：filtered-relation 复用分支把 `a in reuse and j.equals(join)` 改成 `or`。于是只要 alias 在 reuse 集合中（即便 join 不等）或 join 弱相等（即便不在 reuse 集合）就被纳入复用，匹配到不该复用的 alias，多个 FilteredRelation 互相串用 join，结果错乱。模拟 and/or 混淆。

### Group B — 替换/保留机制（A1 接口契约：强比较）
**最终 mutation**：
```diff
-                a for a, j in self.alias_map.items() if a in reuse and j.equals(join)
+                a for a, j in self.alias_map.items() if a in reuse and j == join
```
**变异语义**：把 filtered-relation 分支的弱比较 `j.equals(join)`（忽略 filtered_relation）换成强比较 `j == join`（计入 filtered_relation）。两个不同 condition 的 FilteredRelation 的 join 强比较不相等，无法在该分支正确复用/区分，回到"被忽略"的原 bug。这是 golden 修复的直接逆操作（原 A/B/C 即此），保留为 B 的代表。

### Group C — 替换（C1 类型/数据形状：身份比较）
```diff
-                a for a, j in self.alias_map.items() if a in reuse and j.equals(join)
+                a for a, j in self.alias_map.items() if a in reuse and j is join
```
**变异语义**：用 `is`（对象身份）替代 `j.equals(join)`（值/弱相等）。`alias_map` 中的 Join 对象与传入的 `join` 几乎不会是同一对象，`is` 恒假 → 该分支永远找不到可复用 alias，行为退化。模拟"用 is 比较两个等价对象"的身份/值混淆。

### Group D — 替换（E2 接口契约：关闭 filtered-relation 标志）
**原**：把双分支合并成单分支用 `j.equals`。
**最终 mutation**：
```diff
                     split_subq=False,
-                    reuse_with_filtered_relation=True,
+                    reuse_with_filtered_relation=False,
```
**变异语义**：`build_filtered_relation_q` 调用 `build_filter` 时传 `reuse_with_filtered_relation=False`，使整条链路不进入 filtered-relation 专用复用逻辑——`join` 走普通 `j == join` 分支、`setup_joins` 的 reuse 计算也不强制 can_reuse。多个 FilteredRelation 退回被忽略的旧行为。模拟"调用时把开关传错"的契约破坏，作用点在调用处而非 join 内部。

### Group E — 保留（D-状态：交换两分支比较）
```diff
         if reuse_with_filtered_relation and reuse:
             reuse_aliases = [
-                a for a, j in self.alias_map.items() if a in reuse and j.equals(join)
+                a for a, j in self.alias_map.items() if a in reuse and j == join
             ]
         else:
             reuse_aliases = [
                 a
                 for a, j in self.alias_map.items()
-                if (reuse is None or a in reuse) and j == join
+                if (reuse is None or a in reuse) and j.equals(join)
             ]
```
**变异语义**：把两个分支的比较运算对调——filtered-relation 分支用强比较 `==`、普通分支用弱比较 `equals`。两个分支的语义恰好互换，filtered-relation 场景失去弱比较能力。保留。

## 新设计 Mutation 说明

原 A=B=C 三者完全相同（都 `j.equals`→`j == join`），冗余严重。本次保留该机制为 B，把 A 改为 `and→or`、C 改为 `is` 身份比较、D 改为调用处传 `reuse_with_filtered_relation=False`（不同作用点），E（交换两分支比较）保留。五组覆盖"逻辑运算 / 强比较 / 身份比较 / 关闭开关 / 分支互换"五个角度。全部实测：golden 通过、变异令 F2P（`test_multiple`）失败、`base→golden→test_patch` 后干净应用。
