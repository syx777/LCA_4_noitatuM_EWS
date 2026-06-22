# django__django-14765 Mutation 分析

## 问题背景

PR #14760 之后,所有对 `ProjectState.__init__()` 的调用都已经保证传入的 `real_apps` 参数是一个 `set`。因此 `__init__` 中原本"检查是否为 set,若不是则转换为 set"的兼容逻辑变得多余。本 issue 要求:当 `real_apps` 非 `None` 时,直接 `assert isinstance(real_apps, set)`,把"构造 ProjectState 属于 Django 内部 API"这一契约用断言固化下来,而不是悄悄做类型转换。

## Golden Patch 语义分析

```python
# 修复前
if real_apps:
    self.real_apps = real_apps if isinstance(real_apps, set) else set(real_apps)
else:
    self.real_apps = set()

# 修复后
if real_apps is None:
    real_apps = set()
else:
    assert isinstance(real_apps, set)
self.real_apps = real_apps
```

语义要点有三:
1. 由 `if real_apps:`(真值判断,空集合会落入 else)改为 `if real_apps is None:`(精确判断 None),空 set 现在走 else 分支并通过断言。
2. 不再对非 set 输入做静默 `set(real_apps)` 转换;而是用 `assert isinstance(real_apps, set)` 强制契约。
3. 传入非 set(如 `list`)时应抛出 `AssertionError`。

## 调用链分析

`ProjectState.__init__` 是迁移状态系统的核心构造入口。`real_apps` 用于记录"从主 registry 引入的(通常是未迁移的)app"。F2P 测试 `test_real_apps_non_set` 直接构造 `ProjectState(real_apps=['contenttypes'])` 并断言抛出 `AssertionError`。该断言是唯一被测的行为契约,因此所有有效变异都必须让"传 list 时不再抛 AssertionError"。同时大量 P2P 测试(如 `test_real_apps`)会传入合法的 set,变异不得破坏这些正常路径。

## 替换决策总览

| 组 | 类别 | 决策 | 原因 |
|----|------|------|------|
| A | 🔴 golden-revert | 替换 | `set(real_apps)` 恢复了修复前的静默转换,等价于直接回退 golden |
| B | 🔴 无效(破坏 P2P) | 替换 | `assert not isinstance(...set)` 使合法 set 输入断言失败,破坏 7 个 P2P 测试 |
| C | 🟢 保留 | 保留 | 删除整个 else 分支,自然的"漏写校验",多行改动 |
| D | 🔴 冗余 | 替换 | `else: pass` 与 C(删除 else)功能完全等价,属冗余 |
| E | 🟢 保留 | 保留 | 改抛 `TypeError`,异常类型契约改变,自然且隐蔽 |

保留 C、E;替换 A、B、D。设计 3 个正交替换,使其失败机理彼此不同且与 C/E 不同。

## 各组 Mutation 分析

### 组 A (strategy_code A1)

- 原始 diff:`assert isinstance(real_apps, set)` → `real_apps = set(real_apps) if not isinstance(real_apps, set) else real_apps`
- 分类:🔴 MUST REPLACE(golden-revert 冗余)。该写法精确复活了修复前的 `set(real_apps)` 转换逻辑,等同于撤销 golden patch,属于最直接的回退冗余。
- 最终 diff(替换后):

```diff
-            assert isinstance(real_apps, set)
+            assert isinstance(real_apps, (set, list))
```

- 变异语义:把断言的合法类型集合放宽为 `(set, list)`。list 现在通过断言,不再抛 `AssertionError`,F2P 失败;而合法 set 仍通过,P2P 不受影响。模拟开发者"顺手放宽了类型检查"的真实错误。

### 组 B (strategy_code C1)

- 原始 diff:`assert isinstance(real_apps, set)` → `assert not isinstance(real_apps, set)`
- 分类:🔴 MUST REPLACE(无效变异)。取反后合法的 set 输入会触发断言失败,实测破坏 7 个 P2P 测试,违反"P2P 必须全过"的硬约束。
- 最终 diff(替换后):

```diff
-            assert isinstance(real_apps, set)
+            assert hasattr(real_apps, '__iter__')
```

- 变异语义:用鸭子类型(检查 `__iter__`)替代严格的 set 类型检查。list、tuple、set 等任意可迭代对象都通过,list 不再触发 `AssertionError`,F2P 失败;set 仍可迭代,P2P 安全。

### 组 C (strategy_code B2) — 保留

- 原始 diff:删除 `else: assert isinstance(real_apps, set)` 整个分支。
- 分类:🟢 KEEP。多行删除,模拟"忘记加非空输入校验"的自然缺陷;失败机理为"完全无校验"。
- 变异语义:非 None 输入直接赋值给 `self.real_apps`,任何类型都不校验,list 通过,F2P 失败。

### 组 D (strategy_code B3)

- 原始 diff:`assert isinstance(real_apps, set)` → `pass`
- 分类:🔴 MUST REPLACE(冗余)。`else: pass` 与组 C(删除 else)在运行语义上完全等价,失败模式重复,缺乏正交性。
- 最终 diff(替换后):

```diff
-            assert isinstance(real_apps, set)
+            assert not isinstance(real_apps, dict)
```

- 变异语义:把"必须是 set"反转为"只要不是 dict 就行"。list 不是 dict,断言通过,F2P 失败;set 也不是 dict,P2P 安全。模拟开发者把白名单校验误写成黑名单校验。

### 组 E (strategy_code A3) — 保留

- 原始 diff:`assert isinstance(real_apps, set)` → `if not isinstance(real_apps, set): raise TypeError(...)`
- 分类:🟢 KEEP。异常类型契约被改变(`TypeError` 而非 `AssertionError`),非常自然,且能精准绕过 `assertRaises(AssertionError)`。
- 变异语义:list 输入确实抛出异常,但类型为 `TypeError`,导致 F2P 测试以 ERROR 形式失败(期望 AssertionError 未捕获)。

## 新设计 Mutation 说明

### A1 — `isinstance(real_apps, (set, list))`
- 代码分析依据:golden 的核心契约是"仅接受 set"。把元组扩成 `(set, list)` 恰好让测试用例的 list 落入合法集合。
- 位置/模拟错误:就在断言行,模拟"为兼容历史调用而放宽类型"的常见演进错误。
- 为何难检测:对所有合法 set 调用行为完全不变,只有当测试专门传入 list 并断言失败时才暴露;LLM 生成的测试常只覆盖正常 set 路径。
- 验证结果:py_compile OK;`test_real_apps_non_set` FAIL(failures=1),其余 64 测试全过。

### C1 — `hasattr(real_apps, '__iter__')`
- 代码分析依据:`real_apps` 语义上是"app 集合",鸭子类型检查可迭代性看似合理。
- 位置/模拟错误:断言行,模拟"用鸭子类型替代严格类型检查"的 Pythonic 误用。
- 为何难检测:接受任意可迭代对象,正常 set 路径无差异,失败模式与 A1(类型白名单)正交——一个看类型一个看协议。
- 验证结果:py_compile OK;`test_real_apps_non_set` FAIL(failures=1),P2P 全过。

### B3 — `not isinstance(real_apps, dict)`
- 代码分析依据:contract 是 set 白名单;改为 dict 黑名单是典型的校验方向反写。
- 位置/模拟错误:断言行,模拟"白名单写成黑名单"的逻辑错误。
- 为何难检测:set/list 都不是 dict,正常路径无异常;只有"传 list 期望被拒"这一断言能捕获,与 A1/C1 的失败触发条件不同(它依赖排除 dict 的语义)。
- 验证结果:py_compile OK;`test_real_apps_non_set` FAIL(failures=1),P2P 全过。

## 最终五槽汇总

| 槽 | strategy_code | 失败的 F2P 测试 | 失败形式 |
|----|---------------|------------------|----------|
| A | A1 | test_real_apps_non_set | FAIL |
| B | C1 | test_real_apps_non_set | FAIL |
| C | B2 | test_real_apps_non_set | FAIL |
| D | B3 | test_real_apps_non_set | FAIL |
| E | A3 | test_real_apps_non_set | ERROR(TypeError 未被 assertRaises 捕获) |

五个变异均通过真实测试验证:py_compile 通过,各自令唯一 F2P 测试失败,且无 P2P 回归。失败机理覆盖"类型白名单放宽 / 鸭子类型 / 删除校验 / 黑名单反写 / 异常类型替换",彼此正交。
