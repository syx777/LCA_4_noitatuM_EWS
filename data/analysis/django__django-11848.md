# django__django-11848

## 问题背景

`django.utils.http.parse_http_date` 函数在处理 RFC 850 格式的 HTTP 日期时，对两位数年份的解析逻辑是硬编码的：0–69 映射到 2000–2069，70–99 映射到 1970–1999。然而，RFC 7231 规定：若两位数年份解析出的完整年份比当前年份超出50年以上，则应将其解释为过去的年份（即相同年份后缀对应的上一个世纪）。例如，当前年份为2020时，年份70（2020+50=2070 > 2070，边界值）应解析为2070；而年份71（2071 > 2070，超出50年）应解析为1971。Golden patch 将硬编码逻辑替换为基于当前年份动态计算的50年窗口判断。

## Golden Patch 语义分析

Golden patch 的核心修改在 `parse_http_date` 函数的 `if year < 100:` 分支（仅影响 RFC850 两位数年份）：

```python
# 修复后
current_year = datetime.datetime.utcnow().year
current_century = current_year - (current_year % 100)
if year - (current_year % 100) > 50:
    # 两位数年份比当前年后缀多超50年，解读为上个世纪
    year += current_century - 100
else:
    year += current_century
```

关键语义：`year - (current_year % 100)` 计算了"两位数年份相对于当前年份在同一世纪内的偏移"，若偏移 > 50，则认为该年份处于未来50年以外，应解读为过去世纪（加 `current_century - 100`），否则解读为当前世纪（加 `current_century`）。

这与原始代码的本质区别在于：原始代码以固定的70为分界点（过去曾约定1970为基准），而新逻辑以"当前年后50年"为动态分界点。

## 调用链分析

```
parse_http_date(date)
  ├── 正则匹配：RFC1123_DATE / RFC850_DATE / ASCTIME_DATE
  ├── 若 RFC850（两位数年份）: 执行年份扩展逻辑（golden patch 修改处）
  ├── datetime.datetime.utcnow()  ← 获取当前UTC年份
  └── datetime.datetime(year, month, day, hour, min, sec)  ← 构建datetime对象
        └── calendar.timegm(result.utctimetuple())  ← 转换为epoch秒数

parse_http_date_safe(date)
  └── parse_http_date(date)  ← 唯一调用者，静默忽略异常
```

被修改的代码段仅在 `year < 100`（即 RFC850 格式）时执行，其他格式（RFC1123四位年份、ASCTIME四位年份）不受影响。上游调用者为 `parse_http_date_safe`，下游为 `datetime.datetime.utcnow()`（获取参考时间）和 `datetime.datetime(year, ...)`（构建日期对象）。

测试通过 `mock.patch('django.utils.http.datetime.datetime')` 来控制 `utcnow()` 的返回值，使得边界条件可以精确测试。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 新设计 | 设计 | 原 mutations.jsonl 中无 A 组，需新建 |
| B | 新设计 | 设计 | 原 mutations.jsonl 中无 B 组，需新建 |
| C | 新设计 | 设计 | 原 mutations.jsonl 中无 C 组，需新建 |
| D | 🟡 语义浅层 | 保留 | 单行表达式替换，修改位置处于核心条件判断，能模拟真实边界误判 |
| E | 🔴 必须替换 | 替换 | 添加 `strict_rfc7231=False` 参数，默认回退为原始 bug 代码，含 "Legacy behavior" 注释明显暴露意图 |

语义浅层共 1 个（组 D），替换其中最弱的 floor(1/2) = 0 个：无需替换 D。
必须替换：E（共 1 个）。

## 各组 Mutation 分析

### Group A — 新设计（A1）

**原 mutation**：（无，此组为新设计）

**分类**：新设计（路径 A 中缺失 A 组）

**理由**：针对 golden patch 引入的 `datetime.datetime.utcnow().year` 调用，将其替换为 `datetime.datetime.now().year`。这是一个 A1（改变参数/语义）类型的变异：开发者可能忘记应使用 UTC 时间，而习惯性地使用本地时间 `now()`。

**最终 mutation**：
```diff
diff --git a/django/utils/http.py b/django/utils/http.py
index ff2f08ac1e..89fba71444 100644
--- a/django/utils/http.py
+++ b/django/utils/http.py
@@ -176,7 +176,7 @@ def parse_http_date(date):
     try:
         year = int(m.group('year'))
         if year < 100:
-            current_year = datetime.datetime.utcnow().year
+            current_year = datetime.datetime.now().year
             current_century = current_year - (current_year % 100)
             if year - (current_year % 100) > 50:
                 # year that appears to be more than 50 years in the future are
```

**变异语义**：测试通过 `mock.patch('django.utils.http.datetime.datetime')` 并只模拟 `utcnow`，不模拟 `now`。调用 `datetime.datetime.now()` 返回一个 MagicMock，其 `.year` 属性也是 MagicMock。后续 `MagicMock % 100`、`int - MagicMock`、`MagicMock > 50` 等运算均返回 MagicMock（始终为真值），使得 `year += MagicMock - 100` 导致 year 变为 MagicMock，最终 `datetime.datetime(MagicMock, ...)` 抛出 TypeError，被捕获后重新抛出 ValueError，所有 F2P 测试失败。P2P 测试不使用 mock，`now()` 返回真实时间，与 `utcnow()` 年份相同，P2P 测试通过。代码审查中 `utcnow` 与 `now` 的差异极不明显。

---

### Group B — 新设计（B1）

**原 mutation**：（无，此组为新设计）

**分类**：新设计（路径 A 中缺失 B 组）

**理由**：将边界比较从 `> 50` 改为 `>= 50`，即将50年边界改为严格包含50年。这是 B1（引入边界误差）类型的变异：开发者对"超过50年"的语义理解为"大于等于50年"，导致边界年份被错误地归入过去世纪。

**最终 mutation**：
```diff
diff --git a/django/utils/http.py b/django/utils/http.py
index ff2f08ac1e..f465f72734 100644
--- a/django/utils/http.py
+++ b/django/utils/http.py
@@ -178,7 +178,7 @@ def parse_http_date(date):
         if year < 100:
             current_year = datetime.datetime.utcnow().year
             current_century = current_year - (current_year % 100)
-            if year - (current_year % 100) > 50:
+            if year - (current_year % 100) >= 50:
                 # year that appears to be more than 50 years in the future are
                 # interpreted as representing the past.
                 year += current_century - 100
```

**变异语义**：将"超出50年才认为是过去"改为"满50年就认为是过去"，导致边界值（差值恰好等于50）的年份被误分到过去世纪。例如：utcnow=2019时，年份69（69-19=50）原本 ≤ 50 应视为2069，但 `>= 50` 使其被认为 ≥ 50 → +1900 = 1969（错误）。P2P测试中年份37在~2026年（37-26=11，远小于50），不受影响。只有边界测试用例会失败。

---

### Group C — 新设计（C1）

**原 mutation**：（无，此组为新设计）

**分类**：新设计（路径 A 中缺失 C 组）

**理由**：将比较条件中的 `current_year % 100` 错误地替换为 `year % 100`，即对被解析的两位数年份本身取模，而非对当前年份取模。这是 C1（破坏隐式类型/变量约定）变异：开发者混淆了两个变量（`year` 和 `current_year`），导致动态参考点失效。

**最终 mutation**：
```diff
diff --git a/django/utils/http.py b/django/utils/http.py
index ff2f08ac1e..8e6e843ccf 100644
--- a/django/utils/http.py
+++ b/django/utils/http.py
@@ -178,7 +178,7 @@ def parse_http_date(date):
         if year < 100:
             current_year = datetime.datetime.utcnow().year
             current_century = current_year - (current_year % 100)
-            if year - (current_year % 100) > 50:
+            if year - (year % 100) > 50:
                 # year that appears to be more than 50 years in the future are
                 # interpreted as representing the past.
                 year += current_century - 100
```

**变异语义**：由于此处 `year < 100`（进入分支的前提），`year % 100 == year`，故 `year - (year % 100) = year - year = 0`，条件恒为假（`0 > 50` 永不成立）。这意味着所有两位数年份均被归入当前世纪，包括那些应归入上一世纪的年份（如94→2094，70→2070）。P2P 测试中年份 37 的结果不变（0 > 50 = False → +2000 = 2037），P2P 通过。只有应归入1900s的 F2P 测试用例会失败。该变异极难通过代码审查发现，因为 `year % 100` 与 `current_year % 100` 在视觉上非常相似。

---

### Group D — 保留

**原 mutation**：
```diff
diff --git a/django/utils/http.py b/django/utils/http.py
index ff2f08ac1e..810f1aa0ab 100644
--- a/django/utils/http.py
+++ b/django/utils/http.py
@@ -178,7 +178,7 @@ def parse_http_date(date):
         if year < 100:
             current_year = datetime.datetime.utcnow().year
             current_century = current_year - (current_year % 100)
-            if year - (current_year % 100) > 50:
+            if year > 50:
                 # year that appears to be more than 50 years in the future are
                 # interpreted as representing the past.
                 year += current_century - 100
```

**分类**：🟡 语义浅层（单行表达式简化）

**理由**：将动态计算的 `year - (current_year % 100) > 50` 简化为静态阈值 `year > 50`，不是对原始 bug 代码的直接还原（原始代码用 `year < 70`），而是不同的错误逻辑。作为5个 mutation 中唯一的语义浅层类型（floor(1/2)=0），保留此 mutation。该修改位于核心条件判断位置，能模拟开发者忽略动态参考点的真实错误，且与其他 mutation 使用不同的错误模式。

**最终 mutation**（与原相同）：
```diff
diff --git a/django/utils/http.py b/django/utils/http.py
index ff2f08ac1e..810f1aa0ab 100644
--- a/django/utils/http.py
+++ b/django/utils/http.py
@@ -178,7 +178,7 @@ def parse_http_date(date):
         if year < 100:
             current_year = datetime.datetime.utcnow().year
             current_century = current_year - (current_year % 100)
-            if year - (current_year % 100) > 50:
+            if year > 50:
                 # year that appears to be more than 50 years in the future are
                 # interpreted as representing the past.
                 year += current_century - 100
```

**变异语义**：`year > 50` 是与当前年份无关的静态阈值，无论当前年份如何，年份 ≤ 50 都归入当前世纪，> 50 都归入上一世纪。与正确逻辑（`year - (current_year % 100) > 50`）相比，当前年份后缀改变时，边界判断完全不同。对于 utcnow=2019（current_year%100=19），年份69：`69>50=True → +1900=1969`（错误，应为2069）；年份70：`70>50=True → +1900=1970`（恰好正确）。F2P 测试中边界测试失败。

---

### Group E — 替换

**原 mutation（旧 E，替换）**：
```diff
（旧 E 添加 strict_rfc7231=False 参数，默认回退原始 bug，含注释 "Legacy behavior: hardcoded century boundaries"，判定为 🔴 必须替换）
```

**分类**：🔴 必须替换

**理由**：旧 E 通过添加 `strict_rfc7231=False` 默认参数，在默认调用时完全恢复了原始 bug 行为（year<70 → +2000，否则 +1900），这等价于直接还原 base_commit 代码。此外，代码中包含 `# Legacy behavior: hardcoded century boundaries` 注释，在代码审查中会立即暴露变异意图。

**最终 mutation（新 E，E1）**：
```diff
diff --git a/django/utils/http.py b/django/utils/http.py
index ff2f08ac1e..f0f8471f04 100644
--- a/django/utils/http.py
+++ b/django/utils/http.py
@@ -178,7 +178,7 @@ def parse_http_date(date):
         if year < 100:
             current_year = datetime.datetime.utcnow().year
             current_century = current_year - (current_year % 100)
-            if year - (current_year % 100) > 50:
+            if (current_year % 100) - year > 50:
                 # year that appears to be more than 50 years in the future are
                 # interpreted as representing the past.
                 year += current_century - 100
```

**变异语义**：将减法方向反转：原条件判断"被解析年份超出当前年份后缀50年以上"，变异后判断"当前年份后缀超出被解析年份50年以上"。对于当前年份~2026（后缀26），只有当 `26 - year > 50`，即 `year < -24` 时条件才为真，而两位数年份范围0-99中不可能有负数，故条件恒为假。效果与 C 组相同（所有年份归入当前世纪），但错误原因不同（减法方向错误 vs. 变量混淆），且在 `utcnow` 后缀较大时（如后缀 ≥ 50）会产生不同的错误模式。F2P 测试失败原因：所有应为1900s的年份均被错误解析为2000s。P2P 通过。该变异极难察觉，因为 `year - X` 与 `X - year` 在视觉上差异微小。

## 新设计 Mutation 说明

### Group A（A1: 使用 `now()` 替代 `utcnow()`）
基于对测试机制的深层分析：F2P 测试通过 `mock.patch('django.utils.http.datetime.datetime')` 并显式设置 `mocked_datetime.utcnow`，但未设置 `mocked_datetime.now`。将 `utcnow()` 改为 `now()` 使 mock 失效，导致 MagicMock 在年份算术中引发 TypeError。这模拟了开发者对 UTC 约定的疏忽，在代码审查时 `now/utcnow` 差异极小，但通过逻辑推理可证明会导致所有 F2P 测试失败。

### Group B（B1: `>= 50` 替代 `> 50`）
对 RFC 7231 "超过50年" 语义的边界误解：`more than 50 years` 应为严格大于（`> 50`），但开发者可能写成 `>= 50`（大于等于）。此误解只影响差值恰好等于50的边界年份，不影响差值明显小于50的正常年份，因此只有 F2P 边界测试用例失败，P2P 测试通过。

### Group C（C1: `year % 100` 替代 `current_year % 100`）
对两个变量的混淆：`year`（被解析的两位数年份）和 `current_year`（当前UTC年份）在名称上相近，且都在同一代码块中出现。错误地使用 `year % 100` 使动态参考点失效（因 year < 100 时 year%100=year，结果恒为0），本质上是变量选择错误而非逻辑错误，通过浅层代码审查难以发现。

### Group E（E1: 减法方向反转）
对表达式 `year - (current_year % 100) > 50` 中操作数顺序的混淆：开发者可能对"年份偏移"的计算方向理解有误，写成 `(current_year % 100) - year > 50`。这一错误在数学上等价于将"正偏移"变为"负偏移"，对于合法的两位数年份（0-99），当后缀 < 50 时条件永不成立，导致所有年份归入当前世纪。
