# django__django-15128

## 问题背景

`Query.combine`（如 `qs1 | qs2`）合并两个 queryset 时，若两边共享相同的 alias 前缀，可能产生冲突的 alias 改名（如 T4→T5、T5→T6 最终被误算成 T4→T6），触发 `change_aliases` 中的 `AssertionError`（要求 change_map 的键与值不相交）。Golden patch 在 combine 时对 rhs 调 `bump_prefix(self, exclude={initial_alias})`，先错开 rhs 的 alias 前缀（但排除基表 alias），从而避免冲突。

## Golden Patch 语义分析

修复由多个协同部分组成：
1. `combine` 中新增 `initial_alias = self.get_initial_alias()` 与 `rhs.bump_prefix(self, exclude={initial_alias})`：合并前先把 rhs 的别名前缀整体后移，但**排除基表别名**（两边都必须保留同一个基表别名）。
2. `bump_prefix(self, other_query, exclude=None)`：签名增加 `exclude`，默认 `None`→`{}`；在生成新别名映射时 `if alias not in exclude` 跳过被排除的别名。
3. `change_aliases` 的 `assert set(change_map).isdisjoint(change_map.values())`：键值不相交断言，是 bug 的暴露点。

核心语义：**通过提前错开 rhs 前缀（保留基表）来保证 change_map 的键集与值集不相交**。任何破坏"错开"或"排除基表"的改动都会让断言失败。

F2P 测试 `QuerySetBitwiseOperationTests.test_conflicting_aliases_during_combine`：构造会产生别名冲突的两个 queryset，断言 `qs2 | qs1` 与 `qs1 | qs2` 结果一致且不抛 AssertionError。

## 调用链分析

`combine(rhs, connector)` → `get_initial_alias()` → `rhs.bump_prefix(self, exclude={initial_alias})` → `bump_prefix` 内部用列表推导构造 `{alias: '<prefix><pos>' ...}`（带 `if alias not in exclude`）→ `change_aliases(change_map)` → `assert disjoint`。若基表别名未被排除、或前缀未真正错开，键值集合相交，断言炸。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 补充 | 新增 | 原缺 A 组 |
| B | 🟡 | 替换 | 原 B=C 重复（都删 `if alias not in exclude`）；保留此机制作为 B |
| C | 🔴 必须替换 | 替换 | 与 B 字节级重复 |
| D | ➕ 补充 | 新增 | 原缺 D 组 |
| E | 🟢 高质量 | 保留 | 注释掉 `rhs.bump_prefix(...)` 调用，前缀完全不错开 |

原仅有 B/C/E 且 B=C 重复。补齐 A、D，并把重复的 C 改成不同机制。

## 各组 Mutation 分析

### Group A — 补充（A1 接口契约：丢失 exclude 实参）
```diff
-        rhs.bump_prefix(self, exclude={initial_alias})
+        rhs.bump_prefix(self)
```
**变异语义**：调用 `bump_prefix` 时**省略 `exclude` 实参**，于是 `exclude` 取默认 `{}`，基表别名不再被保护，连基表也被改名。基表别名在 lhs/rhs 两侧本应一致，改名后 change_map 键值相交 → `change_aliases` 的 disjoint 断言失败。模拟"调用时漏传新参数、依赖默认值"的契约误用。前缀仍错开，但保护范围丢失——比直接删调用更隐蔽。

### Group B — 替换/保留机制（B2 删除过滤）
```diff
        self.change_aliases({
            alias: '%s%d' % (self.alias_prefix, pos)
            for pos, alias in enumerate(self.alias_map)
-            if alias not in exclude
        })
```
**变异语义**：删掉列表推导里的 `if alias not in exclude` 过滤，使 `exclude`（含基表别名）形同虚设，基表也被纳入改名。与 A 殊途同归地破坏"排除基表"，但作用点在 `bump_prefix` 内部的推导式而非调用处。

### Group C — 替换（D1 状态初始化：覆盖 exclude）
**原**：与 B 重复。
**最终 mutation**：
```diff
        if exclude is None:
            exclude = {}
+        exclude = {}
```
**变异语义**：在 `exclude is None` 的默认处理之后，**无条件把 `exclude` 重置为空字典**。无论调用方传入什么 exclude（哪怕 `{initial_alias}`），都被这行覆盖掉，基表保护失效。模拟"调试时临时塞了一行重置、忘了删"的状态初始化 bug。与 B（删过滤）机制不同——这里保留了过滤逻辑但清空了它的数据。

### Group D — 补充（B3 逻辑反转：过滤取反）
```diff
        self.change_aliases({
            alias: '%s%d' % (self.alias_prefix, pos)
            for pos, alias in enumerate(self.alias_map)
-            if alias not in exclude
+            if alias in exclude
        })
```
**变异语义**：把过滤条件 `not in` 反转为 `in`，于是**只对被排除的别名（基表）生成改名映射**，其余别名反而不改。语义完全颠倒：本该保护的基表被改、本该错开的别名没动，change_map 与实际 alias_map 错位，触发断言/错误结果。模拟 `not in`/`in` 的经典反转。

### Group E — 保留（D-状态：注释掉前缀错开）
```diff
-        rhs.bump_prefix(self, exclude={initial_alias})
+        # rhs.bump_prefix(self, exclude={initial_alias})
```
**变异语义**：整段注释掉 `bump_prefix` 调用，rhs 前缀完全不错开。合并冲突 queryset 时 change_map 直接出现 T5→T6 这类与既有键冲突的映射，disjoint 断言失败。最直接地撤销修复。保留。

## 新设计 Mutation 说明

原始仅 B/C/E 且 B=C 重复。补齐 A（调用处省略 exclude 实参）、D（过滤条件反转），并把重复的 C 改为"重置 exclude 变量"。五组覆盖修复的多个协同点与机制：A 在 combine 调用处、B 删 bump_prefix 内的过滤、C 清空 exclude 数据、D 反转过滤逻辑、E 注释整个 bump_prefix 调用。全部实测：golden 通过、变异令 F2P（`test_conflicting_aliases_during_combine`）失败、`base→golden→test_patch` 后干净应用。
