# django__django-14493 Mutation 分析

## 问题背景

`ManifestStaticFilesStorage` 在 `max_post_process_passes = 0` 时崩溃。`HashedFilesMixin.post_process`
中的 `for i in range(self.max_post_process_passes):` 循环在 `max=0` 时根本不会执行循环体，导致循环后的
`if substitutions:` 语句引用了从未赋值的局部变量 `substitutions`，抛出
`UnboundLocalError: local variable 'substitutions' referenced before assignment`。
用户将 `max_post_process_passes` 设为 0 是为了让 Django 不产生无效 CSS。

## Golden Patch 语义分析

```python
paths = {path: paths[path] for path in adjustable_paths}
+       substitutions = False          # golden 新增：循环前给出默认值

for i in range(self.max_post_process_passes):
    substitutions = False
    ...
    substitutions = substitutions or subst
    if not substitutions:
        break

if substitutions:                      # 当 max=0 时此处不再 UnboundLocalError
    yield 'All', None, RuntimeError('Max post-process passes exceeded.')
```

Golden 仅在循环之前增加一行 `substitutions = False`，保证当循环体一次都不执行（`max=0`）时，
后续 `if substitutions:` 读到的是 `False`，从而既不崩溃也不误报 RuntimeError。

## 调用链分析

`collectstatic.collect()` (collectstatic.py:126) 迭代 `storage.post_process(found_files)`：
- 若产出元素的 `processed` 是 `Exception`（如本处的 RuntimeError），会 `raise processed`，命令失败。
- F2P 测试 `TestCollectionNoPostProcessReplacedPaths.test_collectstatistic_no_post_process_replaced_paths`
  使用 `max_post_process_passes = 0` 的 storage，运行 `collectstatic` 并断言输出包含 `post-processed`。
  任何在 `max=0` 路径上抛出/误报 RuntimeError 或抛出 UnboundLocalError 的改动都会使该测试失败。
- 默认配置 `max_post_process_passes = 5`，绝大多数 P2P 测试走 `max>=1` 路径。

## 替换决策总览

| 组 | 原 diff 语义 | 分类 | 决策 | 最终机制 |
|----|--------------|------|------|----------|
| B | `range(max)` → `range(max + 1)` | 🟢 KEEP | 保留 | 循环上界 off-by-one |
| C | 删除 `substitutions = False`（golden 逆向） | 🔴 REPLACE | 替换 | `range(max or 1)` 零值保护 |
| E | 删除 `substitutions = False`（与 C 字节相同重复） | 🔴 REPLACE | 替换 | 循环前 flag 初值改为 True |

M=0 单 token swap（无纯 shallow），3 个输入中 2 个为 golden 直接逆向/重复，全部需处理：C、E 替换，B 保留。

## 各组 Mutation 分析

### 组 B — KEEP（off-by-one 循环上界）
- 原 diff：`for i in range(self.max_post_process_passes):` → `for i in range(self.max_post_process_passes + 1):`
- 分类：🟢 KEEP。它修改的是循环上界（line 264），与 golden 新增的初始化行不同；不是 golden 的直接逆向。
- 语义：`max=0` 时循环现在会执行 1 次，进行真实替换并将 `substitutions` 置真，循环后 `if substitutions:`
  误报 `RuntimeError('Max post-process passes exceeded.')`。默认 `max=5` 时多跑一轮通常无害，普通测试通过。
- 最终 diff：保留原样（已基于 post-patch HEAD 重新生成干净 diff）。

### 组 C — REPLACE（golden 逆向）
- 原 diff：删除 `substitutions = False`（golden 新增行），是 golden 的精确逆向 → 🔴。
- 替换设计：`for i in range(self.max_post_process_passes):` → `for i in range(self.max_post_process_passes or 1):`
- 变异语义：伪装成「永不跳过 post-processing」的防御式写法。`max=5` 时与原逻辑等价（`5 or 1 == 5`），普通测试全过；
  `max=0` 时 `0 or 1 == 1` 强制多跑一轮真实替换，`substitutions` 为真，循环后误报 RuntimeError，F2P 失败。
  与 B 正交：B 是 `+1`（始终多一轮），C 是仅在零值时短路保护。

### 组 E — REPLACE（与 C 字节相同的重复）
- 原 diff：与 C 完全相同（删除 `substitutions = False`） → 重复 🔴。
- 替换设计：循环前 `substitutions = False` → `substitutions = True`。
- 变异语义：golden 的目的就是给 `max=0` 一个 `False` 默认值；把它写成 `True` 仍能避免 UnboundLocalError
  （变量已定义），但 `max=0` 时循环体不执行、`substitutions` 保持 `True`，循环后 `if substitutions:` 误报
  RuntimeError。`max>=1` 时循环体内 `substitutions = False` 会覆盖该初值，默认测试通过。
  这是一个「初始化值取反」的隐蔽错误，仅在零次迭代的边界被 F2P 捕获。

## 新设计 Mutation 说明

三组失败机制互相正交，提升多样性：
- B：循环上界 +1（迭代次数错误，始终多一轮）。
- C：`max or 1` 仅在零值时短路（条件短路语义）。
- E：循环前 flag 初值错误（状态初始化错误，依赖循环体覆盖）。

## 验证结果（真实运行）

- 基线（golden + test_patch，无 mutation）：F2P 与全模块 `staticfiles_tests.test_storage`
  37 tests 全部 PASS（rc=0）。
- F2P 模块：`staticfiles_tests.test_storage`，新增测试
  `TestCollectionNoPostProcessReplacedPaths.test_collectstatistic_no_post_process_replaced_paths`。
- 每个最终 mutation：`git apply --check` OK、`patch -p1 --dry-run` OK、`py_compile` OK；
  运行全模块后 **仅** F2P 测试 ERROR（`RuntimeError: Max post-process passes exceeded.`），
  其余 36 个 P2P 全部通过。
  - B：FAILED (errors=1)，仅 F2P。
  - C：FAILED (errors=1)，仅 F2P。
  - E：FAILED (errors=1)，仅 F2P。
- 所有 diff 末尾均带换行符。
