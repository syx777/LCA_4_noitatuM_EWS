# django__django-11728 Mutation 分析

## 问题背景

该 issue 修复 `django/contrib/admindocs/utils.py` 中 `replace_named_groups` 与 `replace_unnamed_groups` 两个函数。这两个函数把 URL 正则里的命名组 `(?P<a>...)` 替换为 `<a>`、未命名组 `(...)` 替换为 `<var>`，供 admindocs 的 `simplify_regex` 展示可读的 URL。

原始 bug：当一个捕获组位于 pattern **末尾**（后面没有 `$` 或其它字符）时，内层逐字符循环里 “括号已平衡” 的判断被放在循环体**开头**。由于检测到右括号 `)` 使 `unmatched_open_brackets` 归零是在“写入 prev_char”那一行，而平衡判断在下一次迭代开头才触发——如果 pattern 末尾就是 `)`，循环已经结束，永远不会再进入下一次迭代，于是末尾组没有被记录、不会被替换。

## Golden Patch 语义分析

Golden patch 把“括号平衡则记录并 break”的判断从循环**开头**移到循环**末尾**（处理完当前字符之后），并相应修正切片右界：
- named：`pattern[start:end + idx]` → `pattern[start:end + idx + 1]`（含闭合括号）
- unnamed：`(start, start + 1 + idx)` → `(start, start + 2 + idx)`

这样末尾就是 `)` 的组也能在闭合的那一轮立即被捕获。test_patch 新增了一批不带 `$` 结尾的 pattern 断言。

## 调用链分析

`views.simplify_regex(pattern)` → 依次调用 `replace_named_groups` 和 `replace_unnamed_groups`。F2P 测试 `admin_docs.test_views.AdminDocViewFunctionsTests.test_simplify_regex` 直接对 `simplify_regex` 的输入输出做断言，覆盖命名组、未命名组、嵌套组、带/不带 `$` 等多种 pattern，因此能精确暴露上述任一函数的边界错误。

## 替换决策总览

| 组 | 原类别 | 决策 | 原因 |
|----|--------|------|------|
| A | 🟡 SEMANTIC-SHALLOW（关键控制流） | KEEP | `==0`→`>0` 同时作用于两处平衡判断节点，落在关键控制流上，建模真实边界错误，保留 |
| B | 🟡 SEMANTIC-SHALLOW（孤立 off-by-one） | REPLACE | 全集仅 2 个 shallow（A、B），需替换 floor(2/2)=1 个最弱者；B 为单行孤立 off-by-one，最易被平凡测试捕获，替换 |
| D | 🔴 MUST REPLACE（非自然 artifact） | REPLACE | diff 内含 `# BUG: ...` 注释，review 一眼可见，必须替换 |
| E | 🔴 MUST REPLACE（功能等价 revert + 死参数） | REPLACE | 新增 `strict_trailing_groups=False` 参数永不被置 True，等价于把代码还原为原始 buggy 行为的死守卫，必须替换 |

M（shallow 数）=2 → 替换 floor(2/2)=1 个最弱 shallow（B）+ 全部 🔴（D、E）。共替换 3 个，保留 1 个（A）。

## 各组 Mutation 分析

### Group A —— KEEP

原 diff（保留）：

```diff
-            if unmatched_open_brackets == 0:
+            if unmatched_open_brackets > 0:
                 group_pattern_and_name.append((pattern[start:end + idx + 1], group_name))
                 break
...
-            if unmatched_open_brackets == 0:
+            if unmatched_open_brackets > 0:
                 group_indices.append((start, start + 2 + idx))
                 break
```

分类：🟡 SEMANTIC-SHALLOW，但位于两处关键控制流节点。
理由：把平衡判断取反，使得只有在“尚未平衡”时才记录切片，建模真实的边界判断方向错误，非孤立、非平凡可推。保留。
变异语义：捕获组终止判定方向反转。
验证：F2P FAIL（rc=1）。

### Group B —— REPLACE（NewB）

原 diff（被替换，🟡 最弱 shallow）：

```diff
-                group_pattern_and_name.append((pattern[start:end + idx + 1], group_name))
+                group_pattern_and_name.append((pattern[start:end + idx], group_name))
```

替换理由：孤立单行 off-by-one，最易被平凡测试捕获，且与 golden 的切片修正点重合，偏向直接 revert 味道。

最终 diff（NewB，未命名组切片 off-by-one，失败模式正交于命名组）：

```diff
-                group_indices.append((start, start + 2 + idx))
+                group_indices.append((start, start + 1 + idx))
```

变异语义：B1 off-by-one。未命名组切片右界少 1，丢弃闭合括号，导致 `<var>` 替换截断。
验证：F2P FAIL（rc=1）；全模块仅 `test_simplify_regex` 的未命名组相关子用例失败（4 个 subTest），无 P2P 回归。

### Group D —— REPLACE（NewD）

原 diff（被替换，🔴 含 `# BUG` 注释 artifact）：

```diff
-            if unmatched_open_brackets == 0:
-                group_pattern_and_name.append((pattern[start:end + idx + 1], group_name))
-                break
+            # BUG: Removed the check - now depends on pattern ending with )
+            group_pattern_and_name.append((pattern[start:end + idx + 1], group_name))
+            break
```

替换理由：diff 明文写 `# BUG`，code review 立即识别，属非自然 artifact。

最终 diff（NewD，escape-state 初始化错误，正交于未命名组）：

```diff
-        unmatched_open_brackets, prev_char = 1, None
+        unmatched_open_brackets, prev_char = 1, '\\'
         for idx, val in enumerate(pattern[end:]):
```

变异语义：D1 state init。`replace_named_groups` 中 `prev_char` 初值由 `None` 误设为反斜杠，使紧随其后的嵌套组左括号 `(` 被当作转义字符忽略，括号计数失衡。仅对嵌套命名组（如 `(?P<a>(x|y))`）才暴露。
验证：F2P FAIL（rc=1）；全模块仅 `test_simplify_regex` 嵌套命名组相关子用例失败（4 个 subTest），无 P2P 回归。

### Group E —— REPLACE（NewE）

原 diff（被替换，🔴 功能等价 revert + 死参数）：

```diff
-def replace_named_groups(pattern):
+def replace_named_groups(pattern, strict_trailing_groups=False):
...
-                group_pattern_and_name.append((pattern[start:end + idx + 1], group_name))
+                if strict_trailing_groups:
+                    group_pattern_and_name.append((pattern[start:end + idx + 1], group_name))
+                else:
+                    group_pattern_and_name.append((pattern[start:end + idx], group_name))
```

替换理由：新增参数默认 False 且无任何调用方传 True，else 分支等价于原始 buggy 行为，是带死守卫的 revert。

最终 diff（NewE，dedup 条件组合错误，正交于命名组/切片）：

```diff
-        if prev_end and start > prev_end or not prev_end:
+        if prev_end and start > prev_end:
```

变异语义：B3 条件组合。删除未命名组去重逻辑里的 `or not prev_end` 分支，使 `prev_end` 仍为 `None`（即首个未命名组）时该组被静默跳过、不参与 `<var>` 替换。建模“删掉看似冗余布尔子句”的真实错误，仅当首个匹配是未命名组时失败。
验证：F2P FAIL（rc=1）；全模块仅 `test_simplify_regex` 首未命名组相关子用例失败（4 个 subTest），无 P2P 回归。

## 新设计 Mutation 说明

三个新变异覆盖三条不同机制、产生正交失败面：
- NewB（B 组）：未命名组切片 off-by-one → 破坏未命名 `<var>` 子用例。
- NewD（D 组）：命名组转义状态初始化错误 → 破坏嵌套命名组子用例。
- NewE（E 组）：未命名组去重条件组合错误 → 破坏首个未命名组子用例。

加上保留的 A 组（双节点平衡判定反转），整套 4 个变异分别命中命名组终止、未命名切片、嵌套命名转义、首未命名去重四个不同语义点，集合多样性最大化，且全部经真实测试验证：baseline PASS、各自使 F2P FAIL、全模块仅 F2P 相关子用例失败、无 P2P 回归。
