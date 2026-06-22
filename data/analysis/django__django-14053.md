# django__django-14053 Mutation 分析

## 问题背景

`HashedFilesMixin.post_process()`（被 `ManifestStaticFilesStorage` / `CachedStaticFilesStorage` 使用）在修复嵌套引用问题时改为对文件做多趟（multi-pass）处理。但实现存在缺陷：对同一个原始文件，`post_process()` 会通过 `yield` 多次返回给 `collectstatic` 的 `collect()`。

后果：
1. `collect()` 把 yield 次数当作被 post-process 的文件数，导致 "X files post-processed" 统计错误。
2. 订阅 yield 的子类（如 WhiteNoise、S3 后端）会对同一文件重复做昂贵处理（Brotli 压缩、重复上传）。
3. 即使有意 yield 中间产物，行为也不一致（只 yield 部分中间文件）。

## Golden Patch 语义分析

修复思路：**收集 + 末尾去重单次 yield**。

- 新增 `processed_adjustable_paths = {}`，以**原始路径 `name` 为键**。
- 第一趟（single pass）：只对 **非 adjustable 文件** 或 **异常** 立即 `yield`；adjustable 文件存入 dict（不立即 yield）。
- 后续多趟（`max_post_process_passes`）：不再 `yield`，而是用 `name` 作键**覆盖**写入 dict（hashed_name 可能更新）。
- 末尾：`yield from processed_adjustable_paths.values()`，每个原始文件**只 yield 一次**且为最终 hash 名。

关键不变量：**每个原始文件最多被 yield 一次**。`dict` 以 `name` 为键是去重的核心机制。

## 调用链分析

`collectstatic.Command.collect()` (collectstatic.py:125-138)
→ `storage.post_process(found_files, dry_run)` 生成器
→ 内部两段循环调用 `self._post_process(...)`（真正做 hash/替换/保存）
→ `collect()` 对每个 yield，若 `processed` 真值则 `self.post_processed_files.append(original_path)`。

测试 `TestCollectionManifestStorage.test_post_processing`：调用 `collect()` 得 `stats`，断言
`assertCountEqual(stats['post_processed'], set(stats['post_processed']))`
即 `post_processed` 列表**无重复**。只要 `post_process` 对任一文件 yield 两次（且 processed 为真），列表出现重复，断言失败。这正是 F2P 检测点。

F2P: `staticfiles_tests.test_storage.TestCollectionManifestStorage.test_post_processing`

## 替换决策总览

| 组 | 原策略 | 分类 | 决策 | 最终 strategy_code | F2P |
|----|--------|------|------|-------------------|-----|
| A | 删除 `else`，无条件入 dict | 🟢 KEEP | 保留 | A1 | FAIL ✓ |
| B | `not in`→`in` 单 token | 🟡 SHALLOW | 保留（M=1, floor(1/2)=0） | B1 | FAIL ✓ |
| C | 第一趟 else 中加 `yield` | 🔴 reverse-of-golden | 替换 | C2 | FAIL ✓ |
| D | 第二趟循环加 `yield` | 🔴 等价冗余（恢复原 bug） | 替换 | D2 | FAIL ✓ |
| E | 伪造 `deduplicate_yields` 参数 | 🔴 不自然人工痕迹 | 替换 | E1 | FAIL ✓ |

Shallow 数量 M=1 → 需替换最弱 floor(1/2)=0 个，故 B 保留。🔴 共 3 个（C/D/E）全部替换。

## 各组 Mutation 分析

### Group A — 保留 🟢

原 diff：删除 `else`，使第一趟所有结果（含已 yield 的非 adjustable 文件）都写入 `processed_adjustable_paths`。
分类：🟢 KEEP。这是**控制流结构改动**而非简单 revert——非 adjustable 文件既被立即 yield，又被存入 dict 在末尾再次 yield，造成泄漏式重复。
变异语义：跨"立即 yield / 末尾 yield"两路径的状态泄漏。
最终 diff（已验证）：将 `else:` 缩进块改为无条件 `processed_adjustable_paths[name] = (...)`。
验证：F2P FAIL，full module 仅该测试失败。

### Group B — 保留 🟡（唯一 shallow）

原 diff：`if name not in adjustable_paths` → `if name in adjustable_paths`。
分类：🟡 SHALLOW（单 token），但落在关键控制流的分区判定上，建模真实"运算符方向写反"边界错误。M=1 故不替换。
变异语义：第一趟分区反转——adjustable 文件被立即 yield 且同时收集，非 adjustable 被错误收集。对 adjustable 文件产生与原 bug 相同的重复 yield。
最终 diff = 原 diff（已验证 F2P FAIL）。

### Group C — 替换 🔴 → C2

原 diff：第一趟 `else` 中追加 `yield name, hashed_name, processed`，直接恢复 golden 修复前的 bug（reverse-of-golden 直接冗余）。故替换。
新设计（C2）：**把 dict 的键从原始 `name` 改为 `hashed_name`**（两处赋值同改）。
变异语义：跨多趟，同一源文件的 `hashed_name` 在每趟会变化，dict 以不同 key 累积同一文件的多条记录，末尾 `values()` 多次 yield 该文件。这是**状态键（state-keying）错误**，与其他组失败模式正交。
最终 diff（已验证）：见 JSONL；F2P FAIL，full module 仅 F2P 失败。

### Group D — 替换 🔴 → D2

原 diff：第二趟循环中追加 `yield`，本质恢复原 bug，与 golden 多趟不 yield 的设计相反（功能等价冗余）。故替换。
新设计（D2）：**把累加器从 `dict` 改为 `list`，两处赋值改 `append`，末尾 `yield from ...values()` 改 `yield from ...`**。
变异语义：dict 按 key 覆盖去重，list `append` 保留每次追加；每趟重复 append 同一 adjustable 文件，末尾整体重复 yield。这是**数据结构选型错误**，是非常自然的实现失误，与 C/E 正交。
最终 diff（已验证）：F2P FAIL，full module 仅 F2P 失败。

### Group E — 替换 🔴 → E1

原 diff：给 `post_process` 凭空增加 `deduplicate_yields=False` 形参并写出冗长反逻辑分支——明显的不自然人工痕迹（真实代码不会引入这种参数）。故替换。
新设计（E1）：**第一趟 `or` 条件中 `isinstance(processed, Exception)` 改为 `processed`**（真值判断）。
变异语义：把"是否为异常"误写成"是否已处理"。pass 1 中任何成功处理（`processed=True`）的 adjustable 文件会被立即 yield，同时仍进入末尾 yield 路径，造成重复；真正的 Exception 对象因本身为真值仍恰好被走 yield 分支。语义上混淆"已处理"与"出错"，是可信的重构错误，与 C/D 正交。
最终 diff（已验证）：F2P FAIL，full module 仅 F2P 失败。

## 新设计 Mutation 说明（正交性）

- C2：错误的字典键（hashed_name vs name）→ 多趟键漂移累积重复。
- D2：错误的数据结构（list vs dict）→ 丢失去重语义。
- E1：错误的谓词语义（truthiness vs isinstance Exception）→ 第一趟提前 yield 已处理文件。

三者分别从「状态键」「数据结构」「条件谓词」三个维度复现"重复 yield"症状，彼此及与保留的 A/B 失败机制互不重叠，提升变异集多样性。

## 验证结果汇总

- 基线（golden 无变异）：F2P PASS（rc0）；full module 32 tests OK。
- A1 / B1 / C2 / D2 / E1：均 py_compile OK，单测 F2P FAIL；并行 runner 因 pickle traceback 限制改用 `--parallel=1`，确认 full module 仅 `test_post_processing` 失败，无 P2P 回归。
- 所有 diff 经 `git diff HEAD`（POST-PATCH 内容）生成，上下文与 golden 后状态匹配，可 `patch -p1` 干净应用。
