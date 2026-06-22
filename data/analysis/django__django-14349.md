# django__django-14349 Mutation 分析

## 问题背景

`URLValidator` 在校验 URL 时未拒绝包含控制字符（`\t`、`\r`、`\n`）的输入。这属于 CVE-2021-23336/相关换行注入类问题：`urlsplit` 等下游处理对换行符敏感，攻击者可借助这些字符进行 header/换行注入。Golden patch 在 `URLValidator` 上新增类属性 `unsafe_chars = frozenset('\t\r\n')`，并在 `__call__` 入口（类型检查之后、scheme 检查之前）增加：若 `value` 与 `unsafe_chars` 有交集即抛出 `ValidationError`。

F2P 测试在 `tests/validators/tests.py` 的 `TEST_DATA` 列表中新增 8 个用例（由 `test_validators` 参数化驱动），覆盖 `\n`/`\r`/`\t` 出现在尾部、中间、首部的多种组合，全部期望 `ValidationError`。

## Golden Patch 语义分析

```python
unsafe_chars = frozenset('\t\r\n')
...
if self.unsafe_chars.intersection(value):
    raise ValidationError(...)
```

- 校验逻辑独立于正则：只要 `value` 中**任意位置**存在三种控制字符之一就拒绝。
- 经实测，去掉该守卫后，全部 8 个 F2P 用例的 URL 都会被正则**接受**（regex 中 `[^\s]` 在 `re.IGNORECASE` 下并非以 `re.DOTALL`/多行处理，实际仍能匹配通过），因此这 8 个用例完全依赖该守卫才能被拒绝。

## 调用链分析

`URLValidator.__call__(value)` → 类型检查 → **unsafe_chars 守卫**（新增）→ scheme 检查 → `RegexValidator.__call__` → IDN/IPv6 兜底。守卫位于最前端，是唯一拦截控制字符的节点；数据流上 `value` 原样进入 `intersection`，对位置不敏感。任何改动其判定范围（集合内容、扫描子串、条件极性）都会改变拒绝集合。

## 替换决策总览

| 组 | 原 strategy | 原 diff 摘要 | 分类 | 决策 | 最终 strategy_code |
|----|------------|-------------|------|------|------|
| A | 常量弱化 | `frozenset('\t\r\n')`→`frozenset('\n')` | 🟡 SEMANTIC-SHALLOW | 保留 | A1 |
| B | 条件取反 | `if ...intersection` → `if not ...intersection` | 🔴 MUST REPLACE | 替换 | B1 |
| E | 死守卫禁用修复 | 新增 `check_unsafe_chars=False` 参数并 `and` 短路 | 🔴 MUST REPLACE | 替换 | E1 |

shallow 数量 M=1（仅 A），floor(1/2)=0，故 shallow 不替换，保留 A。

## 各组 Mutation 分析

### 组 A（保留）
- 原 diff：`unsafe_chars = frozenset('\t\r\n')` → `frozenset('\n')`
- 分类：🟡 单 token 常量弱化。
- 理由：保留在关键控制流节点上、建模了一个真实的“集合元素不完整”边界错误；不破坏任何 P2P；仅 `\r`/`\t` 用例失败。属于应保留的 shallow（M=1 floor 为 0）。
- 最终 diff：保持原样（A1）。
- 变异语义：尾随/任意位置的 `\n` 仍被拒绝，但 `\r`、`\t` 不再被识别为不安全字符。

### 组 B（替换）
- 原 diff：将守卫条件取反 `if not self.unsafe_chars.intersection(value)`。
- 分类：🔴 直接冗余（golden 的反向/逻辑颠倒），且导致 84 个 P2P 报错，极易被任意正常 URL 测试捕获，属于不自然的彻底破坏。
- 替换设计（B1）：`intersection(value)` → `intersection(value.strip())`。
- 最终 diff：
```diff
-        if self.unsafe_chars.intersection(value):
+        if self.unsafe_chars.intersection(value.strip()):
```
- 变异语义：开发者“先规整空白再扫描”的常见习惯；`strip()` 会移除尾部 `\n`/`\r`，导致尾随控制字符被误接受，而首/中部字符仍被拒绝。仅 4 个尾随 F2P 用例失败，P2P 全过。

### 组 E（替换）
- 原 diff：`__init__` 增加 `check_unsafe_chars=False` 参数，守卫改为 `if self.check_unsafe_chars and ...`，默认关闭修复。
- 分类：🔴 不自然产物——通过新增默认值 `False` 的死参数禁用修复，明显是“关掉新逻辑”的人工痕迹。
- 替换设计（E1）：`intersection(value)` → `intersection(value[-1:])`。
- 最终 diff：
```diff
-        if self.unsafe_chars.intersection(value):
+        if self.unsafe_chars.intersection(value[-1:]):
```
- 变异语义：只检查**最后一个字符**，建模“危险字符一定在末尾”的范围假设错误。尾随 `\n`/`\r` 仍被拒绝，但首部/中部的 `\r`、`\t` 漏过。失败用例与 A、B 正交。

## 新设计 Mutation 说明（正交性）

三个最终 mutation 的 F2P 失败面互不重叠：

- A1（`frozenset('\n')`）：失败于含 `\r`、`\t` 的全部用例（保留 `\n`）。
- B1（`value.strip()`）：仅失败于尾随 `\n`/`\r`（首/中部字符仍捕获）。
- E1（`value[-1:]`）：仅失败于中部/首部 `\r`、`\t`（尾随字符仍捕获）。

每个都通过典型测试（无 P2P 回归），仅 F2P 子集失败，符合“真实开发者错误 + 难被自动生成测试覆盖”的目标。

## 验证结果（REAL）

- Harness：`cp -r` 基线仓库 → 应用 golden + test_patch（`patch -p1` rc=0）→ commit。
- 基线（无 mutation）：`python3 tests/runtests.py validators --parallel 1` → **OK, rc=0**（19 tests）。
- 各最终 mutation（从 JSONL diff 重新 `git apply` 验证）：

| code | git apply | py_compile | F2P 模块 (`validators`) |
|------|-----------|-----------|------|
| A1 | rc=0 | rc=0 | FAILED rc=1（仅 F2P 用例失败，无 P2P 回归） |
| B1 | rc=0 | rc=0 | FAILED rc=1（4 个尾随用例，P2P 全过） |
| E1 | rc=0 | rc=0 | FAILED rc=1（4 个中/首部用例，P2P 全过） |

F2P 模块 dotted path：`validators`（即 `tests/validators/tests.py`，新增的参数化用例由 `validators.tests.TestValidators.test_validators` 驱动）。
