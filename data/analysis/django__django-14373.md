# django__django-14373 Mutation 分析

## 问题背景
`DateFormat.Y()` 应始终返回零填充的四位年份。原实现 `return self.data.year` 直接返回整数，
对于 year < 1000 不会补零（如 year=1 输出 `1` 而非 `0001`）。

## Golden Patch 语义分析
```python
def Y(self):
    """Year, 4 digits with leading zeros; e.g. '1999'."""
    return '%04d' % self.data.year
```
将裸整数返回改为 `'%04d'` 格式化字符串，强制四位零填充。语义契约：输出永远是 4 字符宽、左侧补 0 的字符串。

## 调用链分析
`dateformat.format(value, 'Y')` → `DateFormat.format()` → 遍历格式字符 → 调用 `self.Y()`。
唯一行为节点即 `Y()` 的返回值。F2P 测试 `test_Y_format_year_before_1000` 断言
`format(datetime(1,1,1),'Y')=='0001'` 且 `format(datetime(999,1,1),'Y')=='0999'`。

## 替换决策总览

| 组 | 原 diff 摘要 | 分类 | 决策 |
|----|--------------|------|------|
| A | `'%04d'` → `'%d'` | 🔴 直接复现原 bug（golden 的反向） | 替换 |
| B | `'%04d'` → `'%03d'` | 🟡 单 token 宽度修改，真实边界错误 | 保留 |
| E | 加 `zero_pad_year` 死守卫禁用修复 | 🔴 不自然 artifact（dead guard） | 替换 |

shallow 数 M：仅 B 为单 token 交换，M=1，floor(1/2)=0 → 不因 shallow 规则替换 B。
A、E 为 🔴 必替换。

## 各组 Mutation 分析

### Group A —— 🔴 替换
- 原 diff：`return '%d' % self.data.year`，对 year=1 输出 `1`。这正是 golden 修复前的 bug 行为，属直接冗余（reverse of golden）。
- 最终 diff（新设计 A_new）：
```python
return str(self.data.year).rjust(4)
```
- 变异语义：开发者误以为 `str.rjust(4)` 会零填充，实际用空格填充。year=1 得 `'   1'`，typical 测试（year≥1000）通过，F2P 失败。失败模式：填充字符错误（空格 vs 0），与 B/E 正交。

### Group B —— 🟡 保留
- 原 diff：`return '%03d' % self.data.year`，模拟把宽度写成 3 而非 4 的真实笔误。
- 保留理由：单 token 但建模真实“位宽”边界错误；M=1 时 floor(1/2)=0 无需替换。
- 变异语义：所有 <1000 年份少补一位，year=1 得 `'001'`，F2P 失败。

### Group E —— 🔴 替换
- 原 diff：引入 `getattr(self,'zero_pad_year',False)` 死守卫，默认分支退回 `str(year)`，等于禁用修复——典型不自然 artifact（dead param/guard）。
- 最终 diff（新设计 E_new）：
```python
year = self.data.year
if year < 100:
    return '%02d' % year
return '%04d' % year
```
- 变异语义：边界条件错误——开发者对“小年份”用了 2 位填充分支。year<100 时只补到 2 位（year=1 → `'01'`），而 100≤year<1000 正确（999 → `'0999'`）。F2P 的 `datetime(1,1,1)` 断言失败，典型大年份测试通过。失败模式仅限 year<100，与 A（填充字符）、B（全 <1000 少一位）正交。

## 新设计 Mutation 说明
- A_new：`str.rjust(4)` 误用，空格填充而非零填充。
- E_new：`year<100` 早返回 2 位填充，制造仅 <100 年份的边界缺陷。
三者失败输入域互不相同，保证检测难度的多样性。

## 实测验证结果
- 基线（golden + test_patch，无变异）：`utils_tests.test_dateformat` 20 tests OK（rc0）。
- A_new：py_compile OK；模块运行 FAILED (failures=1)，仅 F2P 失败。
- B（保留）：py_compile OK；FAILED (failures=1)，仅 F2P 失败。
- E_new：py_compile OK；FAILED (failures=1)，仅 F2P 失败。
所有 P2P 测试均未受影响。
