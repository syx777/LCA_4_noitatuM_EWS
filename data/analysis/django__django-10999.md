# django__django-10999

## 问题背景

`parse_duration()` 在解析部分负时长字符串时结果错误。具体问题：`standard_duration_re` 的 `hours` 组的向前断言（lookahead）为 `(?=\d+:\d+)`，不允许后续分量为负数；同时各分量（hours/minutes/seconds）都各自带有 `-?`，使得 `-15:30` 被解析为 `minutes=-15, seconds=30`，对应 `timedelta(minutes=-15, seconds=30)` 而非期望的 `timedelta(minutes=-15, seconds=-30)`。

Golden patch 的修复方案：在 `standard_duration_re` 中引入独立的 `(?P<sign>-?)` 捕获组，并将 hours/minutes/seconds 改为无符号 `\d+`，使负号统一由 `sign` 字段控制，`parse_duration` 已有的 `sign * datetime.timedelta(**kw)` 逻辑随之正确生效。

## Golden Patch 语义分析

**修复核心**：将分散在各时间分量中的负号（`-?\d+`）集中到一个前置 `(?P<sign>-?)` 组中。

**为什么这样是正确的**：
- 旧行为：`-15:30` → `minutes='-15', seconds='30'` → `timedelta(minutes=-15, seconds=30)` = `-14:30`（错误）
- 新行为：`-15:30` → `sign='-', minutes='15', seconds='30'` → `(-1) * timedelta(minutes=15, seconds=30)` = `-15:30`（正确）
- 关键细节：`days` 分量保留自己的 `-?` 独立负号（如 `-4 15:30` 中 days=-4，而 sign=+1），这两个负号语义独立、互不干扰。

**sign 的作用范围**：只影响 hours/minutes/seconds/microseconds，不影响 days。`days` 通过 `(?P<days>-?\d+)` 独立捕获并单独计算 `datetime.timedelta`。

## 调用链分析

```
DurationField.to_python() → parse_duration(value)
DurationField (forms/fields.py) → parse_duration(str(value))
```

`parse_duration` 是纯函数，无副作用，三路正则（standard / iso8601 / postgres）顺序尝试匹配，命中后统一走一套 `kw` 处理逻辑：
1. 弹出 `days` → `timedelta(days=...)`
2. 弹出 `sign` → `+1` 或 `-1`
3. 处理 microseconds 的 ljust 填充（以及遗留的负号传播，patched 后为死代码）
4. 剩余 kw 转 float
5. 返回 `days + sign * timedelta(**kw)`

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 直接删除 sign 组（`(?P<sign>-?)` 行置空），等同于还原 regex 修复的核心部分 |
| B | 🟡 语义浅层 | 保留 | `==` → `!=`，位于关键逻辑节点，能模拟真实符号误写，且为组内唯一语义浅层 |
| C | 🔴 必须替换 | 替换 | 与 D 完全相同（重复 diff），且都是删除 `sign *`，等同于直接还原修复效果 |
| D | 🔴 必须替换 | 替换 | 与 C 完全相同（重复 diff），直接删除 sign 乘法 |
| E | 🔴 必须替换 | 替换 | 添加无意义 `strict=False` 参数并反转默认 sign 逻辑，人工痕迹明显（代码审查即可发现） |

语义浅层共 1 个（B），替换 floor(1/2) = 0 个：B 保留。

## 各组 Mutation 分析

### Group A — 替换
**原 mutation**：
```diff
-    r'(?P<sign>-?)'
+    
```
**分类**：🔴 必须替换
**理由**：直接删除 `(?P<sign>-?)` 行，使 `sign` 捕获组消失，`parse_duration` 中 `kw.pop('sign', '+')` 永远返回默认 `'+'`，sign 始终为 +1，所有通过 sign 机制处理的负时长全部失效。这是对 golden patch regex 修复的直接逆操作。

**最终 mutation**：
```diff
diff --git a/django/utils/dateparse.py b/django/utils/dateparse.py
index f90d952581..2272802081 100644
--- a/django/utils/dateparse.py
+++ b/django/utils/dateparse.py
@@ -28,7 +28,7 @@ datetime_re = re.compile(
 
 standard_duration_re = re.compile(
     r'^'
-    r'(?:(?P<days>-?\d+) (days?, )?)?'
+    r'(?:(?P<days>\d+) (days?, )?)?'
     r'(?P<sign>-?)'
     r'((?:(?P<hours>\d+):)(?=\d+:\d+))?'
     r'(?:(?P<minutes>\d+):)?'
```
**变异语义**：将 `days` 的 `-?\d+` 改为 `\d+`，使得带有负 days 的 `D HH:MM:SS` 格式（如 `-4 15:30`）无法匹配 `standard_duration_re`。模拟真实开发者错误：在引入 `(?P<sign>-?)` 中心化负号后，误认为 `days` 分量也应改为无符号——忽略了 `days` 的负号与 `sign` 组语义上是相互独立的（`days` 可以为负而 `sign` 仍为 `+1`）。所有不含冒号的纯天数负时长（`-172800`）和2/3组件负时长（`-15:30`、`-1:15:30`）不受影响；只有 `D HH:MM` 格式中 days 为负时才失败。简单测试难以覆盖，代码审查不易察觉。

---

### Group B — 保留
**原 mutation**：
```diff
-        sign = -1 if kw.pop('sign', '+') == '-' else 1
+        sign = -1 if kw.pop('sign', '+') != '-' else 1
```
**分类**：🟡 语义浅层（保留）
**理由**：`==` → `!=` 在关键逻辑节点，完全反转 sign 逻辑，使所有负时长解析为正、正时长解析为负。虽是单符号改动，但位于 sign 判断的核心处，能模拟逻辑运算符误写，是组内唯一语义浅层 mutation，按规则保留。

**最终 mutation**（与原相同）：
```diff
diff --git a/django/utils/dateparse.py b/django/utils/dateparse.py
index f90d952581..73dc5debae 100644
--- a/django/utils/dateparse.py
+++ b/django/utils/dateparse.py
@@ -138,7 +138,7 @@ def parse_duration(value):
     if match:
         kw = match.groupdict()
         days = datetime.timedelta(float(kw.pop('days', 0) or 0))
-        sign = -1 if kw.pop('sign', '+') == '-' else 1
+        sign = -1 if kw.pop('sign', '+') != '-' else 1
```
**变异语义**：sign 判断逻辑完全反转。负时长输入 sign='-' → `'-' != '-'` = False → sign=+1（错误），所有负时长变为正。正时长 sign='' → `'' != '-'` = True → sign=-1（错误），所有正时长变为负。所有正负时长测试全部失败。

---

### Group C — 替换
**原 mutation**：
```diff
-        return days + sign * datetime.timedelta(**kw)
+        return days + datetime.timedelta(**kw)
```
**分类**：🔴 必须替换（与 D 完全相同，为重复 diff）
**理由**：删除 `sign *`，使 sign 变量被计算但从未使用，等价于 sign 始终为 +1。功能上等同于还原修复效果，且与 D 完全重复。

**最终 mutation**：
```diff
diff --git a/django/utils/dateparse.py b/django/utils/dateparse.py
index f90d952581..fb302faafe 100644
--- a/django/utils/dateparse.py
+++ b/django/utils/dateparse.py
@@ -143,5 +143,6 @@ def parse_duration(value):
             kw['microseconds'] = kw['microseconds'].ljust(6, '0')
         if kw.get('seconds') and kw.get('microseconds') and kw['seconds'].startswith('-'):
             kw['microseconds'] = '-' + kw['microseconds']
+        seconds = float(kw.pop('seconds', 0) or 0)
         kw = {k: float(v) for k, v in kw.items() if v is not None}
-        return days + sign * datetime.timedelta(**kw)
+        return days + sign * datetime.timedelta(**kw) + datetime.timedelta(seconds=seconds)
```
**变异语义**：在 `sign * timedelta(**kw)` 计算前将 `seconds` 单独弹出，使 `sign` 只作用于 hours 和 minutes，`seconds` 始终以正值添加。对于 `-15:30`（sign=-1, minutes=15, seconds=30）：结果 = `(-1)*timedelta(minutes=15) + timedelta(seconds=30)` = `timedelta(minutes=-15, seconds=30)` = `-14:30`，而非期望的 `-15:30`。模拟真实开发者错误：误认为 seconds 会通过 timedelta 的进位机制自动处理，只需对 minutes 取反。只有包含秒的负时长测试会失败，正时长完全不受影响，代码结构看似合理。

---

### Group D — 替换
**原 mutation**：
```diff
-        return days + sign * datetime.timedelta(**kw)
+        return days + datetime.timedelta(**kw)
```
**分类**：🔴 必须替换（与 C 完全相同）
**理由**：与 C 的 diff 完全一致，重复 mutation，且为直接还原修复的核心 `sign *` 乘法。

**最终 mutation**：
```diff
diff --git a/django/utils/dateparse.py b/django/utils/dateparse.py
index f90d952581..84557a8f0d 100644
--- a/django/utils/dateparse.py
+++ b/django/utils/dateparse.py
@@ -29,8 +29,7 @@ datetime_re = re.compile(
 standard_duration_re = re.compile(
     r'^'
     r'(?:(?P<days>-?\d+) (days?, )?)?'
-    r'(?P<sign>-?)'
-    r'((?:(?P<hours>\d+):)(?=\d+:\d+))?'
+    r'((?:(?P<sign>-?)(?P<hours>\d+):)(?=\d+:\d+))?'
     r'(?:(?P<minutes>\d+):)?'
     r'(?P<seconds>\d+)'
     r'(?:\.(?P<microseconds>\d{1,6})\d{0,6})?'
```
**变异语义**：将独立的 `(?P<sign>-?)` 组合并到 `hours` 的可选捕获组内，使 `sign` 只在三分量格式（H:M:S）中被捕获，而在两分量（M:S）和一分量（S）格式中，`sign` 为 `None`，`kw.pop('sign', '+')` 返回 `None`，`None == '-'` 为 False → sign=+1。结果：`-15:30`、`-01:01`、`-172800`、`-30.1` 等两/一分量负时长全部解析为正值；仅三分量格式（`-1:15:30`）因 sign 在 hours 组内正确捕获而不受影响。模拟真实开发者误解：认为 sign 是小时分量的前缀而非整个时间部分的前缀。难以发现，因为 3 分量测试仍通过，需要 2 分量负时长测试才能检测。

---

### Group E — 替换
**原 mutation**：
```diff
-def parse_duration(value):
+def parse_duration(value, strict=False):
...
-        sign = -1 if kw.pop('sign', '+') == '-' else 1
+        sign_str = kw.pop('sign', '+')
+        if strict:
+            sign = -1 if sign_str == '-' else 1
+        else:
+            sign = -1 if sign_str == '+' else 1
```
**分类**：🔴 必须替换（不自然，有明显人工痕迹）
**理由**：添加无实际语义的 `strict=False` 参数，在 `strict=False`（默认）时反转 sign 逻辑（判断 `== '+'` 而非 `== '-'`），使所有调用者（未传参）默认进入错误分支。`strict` 参数在整个 django 代码库中无对应使用，且默认值使行为错误，代码审查者立即能识别其人工性。

**最终 mutation**：
```diff
diff --git a/django/utils/dateparse.py b/django/utils/dateparse.py
index f90d952581..4d58101a4c 100644
--- a/django/utils/dateparse.py
+++ b/django/utils/dateparse.py
@@ -141,7 +141,7 @@ def parse_duration(value):
         sign = -1 if kw.pop('sign', '+') == '-' else 1
         if kw.get('microseconds'):
             kw['microseconds'] = kw['microseconds'].ljust(6, '0')
-        if kw.get('seconds') and kw.get('microseconds') and kw['seconds'].startswith('-'):
+        if sign == -1 and kw.get('microseconds'):
             kw['microseconds'] = '-' + kw['microseconds']
         kw = {k: float(v) for k, v in kw.items() if v is not None}
         return days + sign * datetime.timedelta(**kw)
```
**变异语义**：将原本的死代码条件（patched 状态下 `kw['seconds'].startswith('-')` 永远为 False）改为 `sign == -1 and kw.get('microseconds')`，使所有带小数秒的负时长错误地对 microseconds 字符串追加 `'-'` 前缀。例如 `-30.1`：microseconds 经 ljust 为 `'100000'`，再追加 `'-'` 变为 `'-100000'`，`float('-100000')=-100000.0`，最终 `sign * timedelta(seconds=30, microseconds=-100000)` = `timedelta(seconds=-30, microseconds=100000)` ≠ 期望 `timedelta(seconds=-30, microseconds=-100000)`。模拟真实开发者错误：看到遗留的 microseconds 符号传播代码，试图用更简洁的 `sign == -1` 替换已失效的 `startswith('-')` 判断，但忽略了此时 microseconds 已经是正值、再次取反会方向错误。仅在负时长+小数秒的场景下失败，正时长和整秒负时长不受影响。

## 新设计 Mutation 说明

### Mutation A 设计依据
基于调用链分析：golden patch 在 `standard_duration_re` 中引入 `(?P<sign>-?)` 的同时，保留了 `(?P<days>-?\d+)` 中 days 的独立负号。二者语义独立：sign 控制 H:M:S 部分，days 的负号直接体现在 timedelta(days=...) 的负数值中。自然的开发者错误是：在"集中化负号"的思路下，顺手将 days 也改为无符号，忽略了 `-4 15:30`（负 days + 正 time）场景。

### Mutation C 设计依据
`parse_duration` 中 `sign * datetime.timedelta(**kw)` 对 **所有** 非 days 字段均匀取反。设计思路：在清理 kw 字典时提前弹出 seconds，使其不参与 sign 乘法，单独以正值加入结果。模拟开发者的误解：以为 timedelta 内部的进位机制会自动处理 seconds 的符号（类似 "minutes=-15, seconds=30 就是 -14:30"），只需对 minutes/hours 取反。这是一个贴近旧版本行为（`-15:30` 旧期望是 `timedelta(minutes=-15, seconds=30)`）的 mutation，能欺骗对旧测试行为有印象的审查者。

### Mutation D 设计依据
golden patch 将 `(?P<sign>-?)` 提取为独立行（在 days 之后、hours 之前）。设计思路：将 sign 合并回 hours 的可选捕获组内，使其成为 hours 前缀的一部分。这个修改看似是"把 sign 和 hours 放在一起更整洁"，但语义上错误：sign 应在 hours 缺席时依然能捕获负号。从 2 分量测试（`-15:30`）的角度看，sign 在 hours 可选组内时将捕获失败，导致符号丢失。而 3 分量测试（`-1:15:30`）仍正确，制造了"大部分测试通过"的假象。

### Mutation E 设计依据
golden patch 后 `kw['seconds'].startswith('-')` 是死代码（patched 状态 seconds 为无符号）。设计思路：激活这段死代码，将触发条件改为更"合理"的 `sign == -1 and kw.get('microseconds')`，看似在修复遗留代码但实际引入新 bug：microseconds 已经是正数（经 ljust 填充），再次对其添加 `'-'` 前缀导致 `float('-100000') = -100000.0`，最终计算方向错误。只在 `-30.1` 这类带小数秒的负时长场景下失败，其他情况完全正确，是最具隐蔽性的 mutation。
