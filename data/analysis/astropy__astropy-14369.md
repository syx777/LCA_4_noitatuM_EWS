# astropy__astropy-14369

## 问题背景

CDS 格式（天文星表标准）的单位解析器在处理复合单位时存在 bug。当用户读取 MRT 文件中的单位字符串（如 `km/s/Mpc`、`10+3J/m/s/kpc2`）时，astropy 的 CDS 解析器会错误地解析链式除法。

根本原因：在 PLY（Python Lex-Yacc）语法规则 `p_division_of_units` 中，三元产生式的操作数顺序写反了：

- **base_commit（错误）**：`unit_expression DIVISION combined_units`
  - 解析 `km/s/Mpc` 时：`km / (s/Mpc)` = `km*Mpc/s`（错误！）
- **golden patch（正确）**：`combined_units DIVISION unit_expression`
  - 解析 `km/s/Mpc` 时：`(km/s) / Mpc` = `km/s/Mpc`（正确！）

golden patch 还同时更新了 `cds_parsetab.py`（PLY 自动生成的解析表），使其与新语法规则一致，并修正了文档中的一个 URL。

## Golden Patch 语义分析

修复的核心语义：CDS 单位格式中的除法应当是**左结合**的（left-associative），即 `a/b/c = (a/b)/c = a/(b*c)`。

在 PLY 语法中，左结合除法需要将**已归约的左侧复合单位**（`combined_units`）作为被除数，而**右侧的单个单位表达式**（`unit_expression`）作为除数。原来的写法 `unit_expression DIVISION combined_units` 将右侧设为 `combined_units`，导致右结合行为：`a/(b/c) = a*c/b`（错误）。

修复不仅改变了语法规则字符串（PLY 通过 docstring 解析语法），还必须同步更新预生成的解析表 `cds_parsetab.py`，否则 PLY 会检测到 signature 不匹配并重新生成（或报错）。

## 调用链分析

```
CDS.parse(s)
  └── cls._parser.parse(s, lexer=cls._lexer)  # PLY LALR(1) 解析器
        ├── p_main()                           # 顶层规则：factor combined_units | combined_units | ...
        │     └── 调用 Unit(p[1] * p[2]) 或 Unit(p[1])
        ├── p_combined_units()                 # 透传：product_of_units | division_of_units
        ├── p_product_of_units()               # unit_expression PRODUCT combined_units -> p[1]*p[3]
        │                                      # unit_expression -> p[1]
        ├── p_division_of_units()              # ★ 被修复的函数 ★
        │     ├── DIVISION unit_expression     # /s -> s^-1 (2-token)
        │     └── combined_units DIVISION unit_expression  # km/s -> km/s (3-token)
        ├── p_unit_expression()                # unit_with_power | (combined_units)
        ├── p_unit_with_power()                # UNIT numeric_power -> unit^power
        ├── p_numeric_power()                  # sign UINT -> sign*uint
        ├── p_sign()                           # SIGN | empty -> ±1.0
        ├── p_factor()                         # 10+3 等数值因子
        └── p_signed_float/int()               # 带符号数值
```

数据流：字符串 `km/s/Mpc` → lexer 分词为 `[UNIT(km), DIVISION, UNIT(s), DIVISION, UNIT(Mpc)]` → LALR(1) 解析器按语法规则归约 → `p_division_of_units` 被调用两次 → 最终返回 `Unit` 对象。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 新设计 | 新增 | 原数据中无 A 组，需新建高质量 mutation |
| B | 语义浅层（`==` → `>`） | 保留 | 修改位置在关键条件判断节点，能模拟真实边界错误 |
| C | 🔴 必须替换 | 替换 | 直接冗余：完全还原 base_commit 的原始 bug（golden patch 的逆操作） |
| D | 新设计 | 新增 | 原数据中无 D 组，需新建高质量 mutation |
| E | 新设计 | 新增 | 原数据中无 E 组，需新建高质量 mutation |

语义浅层共 1 个（B），保留（无需替换）。
原始数据只有 B、C 两组，需新建 A、D、E 三组，并替换 C 组。

## 各组 Mutation 分析

### Group A — 新建

**原 mutation**：无（原数据中不存在 A 组）

**分类**：🟢 新设计

**理由**：在 `p_product_of_units` 中将乘法操作改为除法，模拟开发者在实现 PRODUCT 语义时误用除法运算符。

**最终 mutation**：
```diff
diff --git a/astropy/units/format/cds.py b/astropy/units/format/cds.py
index 8a23bf8260..e264653c30 100644
--- a/astropy/units/format/cds.py
+++ b/astropy/units/format/cds.py
@@ -174,7 +174,7 @@ class CDS(Base):
                              | unit_expression
             """
             if len(p) == 4:
-                p[0] = p[1] * p[3]
+                p[0] = p[1] / p[3]
             else:
                 p[0] = p[1]
```

**变异语义**：将 `product_of_units` 的三元归约动作从乘法改为除法。当解析 `km.s-1` 时，原本应归约为 `km * s^-1 = km/s`，变异后变为 `km / s^-1 = km*s`（完全错误）。对于 `km/(s.Mpc)` 中的括号内部 `s.Mpc`，原本归约为 `s*Mpc`，变异后变为 `s/Mpc`，导致最终结果 `km/(s/Mpc) = km*Mpc/s`（错误）。

此 mutation 难以发现的原因：`p_product_of_units` 函数看起来与 `p_division_of_units` 结构相似，开发者在 code review 时可能混淆两个函数的语义；`*` 和 `/` 在视觉上相近，单行 diff 不易察觉。

会失败的测试：`km.s-1`（P2P）、`km/(s.Mpc)`（F2P）、`mW/m2`（P2P，因括号内 m2 不受影响但 mW.m2 相关测试可能失败）。

---

### Group B — 保留

**原 mutation**：
```diff
diff --git a/astropy/units/format/cds.py b/astropy/units/format/cds.py
index 8a23bf826..6ac0044b3 100644
--- a/astropy/units/format/cds.py
+++ b/astropy/units/format/cds.py
@@ -183,7 +183,7 @@ class CDS(Base):
             division_of_units : DIVISION unit_expression
                               | combined_units DIVISION unit_expression
             """
-            if len(p) == 3:
+            if len(p) > 3:
                 p[0] = p[2] ** -1
             else:
                 p[0] = p[1] / p[3]
```

**分类**：🟡 语义浅层（`==` → `>`）— 保留

**理由**：修改位置在 `p_division_of_units` 的核心分支判断处，是整个 bug 修复的关键逻辑节点。`len(p) == 3` 区分两个产生式（2-token 的 `/unit` 与 3-token 的 `a/b`），改为 `> 3` 会导致 2-token 规则（`len=3`）走错分支，尝试访问不存在的 `p[3]` 从而引发 IndexError。这模拟了开发者对 PLY `len(p)` 语义的误解（PLY 的 `p` 比符号数多 1）。

**最终 mutation**（与原相同）：
```diff
diff --git a/astropy/units/format/cds.py b/astropy/units/format/cds.py
index 8a23bf826..6ac0044b3 100644
--- a/astropy/units/format/cds.py
+++ b/astropy/units/format/cds.py
@@ -183,7 +183,7 @@ class CDS(Base):
             division_of_units : DIVISION unit_expression
                               | combined_units DIVISION unit_expression
             """
-            if len(p) == 3:
+            if len(p) > 3:
                 p[0] = p[2] ** -1
             else:
                 p[0] = p[1] / p[3]
```

**变异语义**：`len(p) > 3` 在 2-token 规则（`len=3`）时为 False，导致执行 `else` 分支，尝试 `p[1] / p[3]`，但 `p[3]` 不存在（IndexError）。`/s` 和 `/m3` 等前置除法单位会解析失败。在 3-token 规则（`len=4`）时 `> 3` 为 True，反而执行 `p[2] ** -1`（错误地把 `combined_units/unit` 当成 `/unit` 处理），导致 `km/s` 变为 `s^-1`（丢失分子）。

---

### Group C — 替换

**原 mutation**：
```diff
diff --git a/astropy/units/format/cds.py b/astropy/units/format/cds.py
index 8a23bf826..4b1d7c16a 100644
--- a/astropy/units/format/cds.py
+++ b/astropy/units/format/cds.py
@@ -181,7 +181,7 @@ class CDS(Base):
         def p_division_of_units(p):
             """
             division_of_units : DIVISION unit_expression
-                              | combined_units DIVISION unit_expression
+                              | unit_expression DIVISION combined_units
             """
```
（以及对应的 cds_parsetab.py 变更）

**分类**：🔴 必须替换

**理由**：这是 golden patch 的完全逆操作，将语法规则从已修复的 `combined_units DIVISION unit_expression` 改回 base_commit 的错误版本 `unit_expression DIVISION combined_units`。属于直接冗余，不具备独立的变异价值。

**最终 mutation**（替换为新设计）：
```diff
diff --git a/astropy/units/format/cds.py b/astropy/units/format/cds.py
index 8a23bf8260..b937698c07 100644
--- a/astropy/units/format/cds.py
+++ b/astropy/units/format/cds.py
@@ -184,7 +184,7 @@ class CDS(Base):
                               | combined_units DIVISION unit_expression
             """
             if len(p) == 3:
-                p[0] = p[2] ** -1
+                p[0] = p[2]
             else:
                 p[0] = p[1] / p[3]
```

**变异语义**：在 2-token 规则（`DIVISION unit_expression`，即前置 `/`）中，去掉了 `** -1` 幂运算，使 `/s` 解析为 `s`（而非 `s^-1`）。这模拟了开发者在实现"前置除法"语义时忘记取倒数的错误——直觉上 `/s` 就是 `s` 的某种变体，而忘记了它表示 `s^-1`。

会失败的测试：`/s`（F2P）、`1.5×10+11/m`（F2P，因为 `/m` 变为 `m` 而非 `m^-1`，导致 `1.5e11*m` 而非 `1.5e11/m`）、`/m3`（P2P，`m^-3` 变为 `m^3`）。难以发现的原因：grammar docstring 看起来完全正确，只有 action 代码有微小差异（去掉了 `** -1`），代码审查者需要仔细核对语义才能发现。

---

### Group D — 新建

**原 mutation**：无（原数据中不存在 D 组）

**分类**：🟢 新设计

**理由**：在 `p_main` 中将 `factor * combined_units` 改为 `factor / combined_units`，模拟开发者在组合数值因子与单位时误用除法。

**最终 mutation**：
```diff
diff --git a/astropy/units/format/cds.py b/astropy/units/format/cds.py
index 8a23bf8260..30cce4424a 100644
--- a/astropy/units/format/cds.py
+++ b/astropy/units/format/cds.py
@@ -155,7 +155,7 @@ class CDS(Base):
             from astropy.units.core import Unit
 
             if len(p) == 3:
-                p[0] = Unit(p[1] * p[2])
+                p[0] = Unit(p[1] / p[2])
             elif len(p) == 4:
                 p[0] = dex(p[2])
             else:
```

**变异语义**：`p_main` 的 `len(p) == 3` 分支处理 `factor combined_units`（如 `10+3J/m/s/kpc2` 中的 `10^3` 乘以 `J/m/s/kpc2`）。变异后变为 `factor / combined_units`，即 `10^3 / (J/m/s/kpc2)`，结果完全错误。

会失败的测试：`10+3J/m/s/kpc2`（F2P）、`10pix/nm`（P2P）、`1.5x10+11m`（P2P）等所有带数值因子的单位。难以发现的原因：`p_main` 函数处理多种情况，`len(p)==3` 对应 `factor combined_units`，而 `len(p)==4` 对应括号形式，开发者可能在修改时混淆了不同分支的语义；`*` 与 `/` 的单字符差异在复杂函数中不易察觉。

---

### Group E — 新建

**原 mutation**：无（原数据中不存在 E 组）

**分类**：🟢 新设计

**理由**：在 `p_sign` 的空规则（无符号时）中将默认值从 `1.0` 改为 `-1.0`，使所有无显式符号的幂次默认为负，模拟开发者在初始化符号默认值时的错误。

**最终 mutation**：
```diff
diff --git a/astropy/units/format/cds.py b/astropy/units/format/cds.py
index 8a23bf8260..e09888c721 100644
--- a/astropy/units/format/cds.py
+++ b/astropy/units/format/cds.py
@@ -241,7 +241,7 @@ class CDS(Base):
             if len(p) == 2:
                 p[0] = p[1]
             else:
-                p[0] = 1.0
+                p[0] = -1.0
 
         def p_signed_int(p):
             """
```

**变异语义**：`p_sign` 的空产生式（无 SIGN token）原本返回 `1.0`（正号），变异后返回 `-1.0`（负号）。这影响所有使用 `numeric_power` 的单位幂次：

- `kpc2`：`sign=empty=-1.0`，`UINT=2`，`numeric_power = -1.0 * 2 = -2`，即 `kpc^-2` 而非 `kpc^2`
- `m2`：`m^-2` 而非 `m^2`（破坏 P2P 测试）
- `s-1`：`sign=SIGN=-1.0`，不受影响（有显式符号）

会失败的测试：`10+3J/m/s/kpc2`（F2P，`kpc2` 变为 `kpc^-2`）、`m2`（P2P）、`mW/m2`（P2P）等所有含无符号幂次的单位。

难以发现的原因：`p_sign` 是一个辅助函数，处于调用链底层，其默认值 `1.0` 看起来非常"自然"，改为 `-1.0` 后代码仍然语法正确且逻辑结构完整；只有在测试带无符号指数的单位时才会暴露错误。

## 新设计 Mutation 说明

### Mutation A（p_product_of_units 乘改除）

**代码分析基础**：`p_product_of_units` 处理 `unit_expression PRODUCT combined_units`（如 `km.s-1`）和单个 `unit_expression`。三元规则的 action 是 `p[1] * p[3]`，将左右两个单元素合并。将 `*` 改为 `/` 会使 PRODUCT（`.`）操作符语义变为除法。

**选择此位置的原因**：与 `p_division_of_units` 相邻，都是二元单位组合规则，容易发生"复制粘贴错误"——开发者在同时修改两个函数时可能不小心在 product 函数中写了 `/`。

**模拟的真实错误**：在实现 PLY 语法 action 时，开发者可能混淆了 `p_product_of_units` 和 `p_division_of_units` 的运算符，特别是在 code review 时两个函数结构完全相同，只有运算符不同。

### Mutation C（p_division_of_units 2-token 去掉取倒数）

**代码分析基础**：2-token 规则 `DIVISION unit_expression` 对应前置除法如 `/s`，语义是"s 的倒数"，即 `s^-1`。去掉 `** -1` 后变为直接返回 `p[2]`（即 `s` 本身）。

**选择此位置的原因**：golden patch 修复的是 3-token 规则的语法，而 2-token 规则的 action 在修复前后都是正确的。对 2-token action 的修改与 golden patch 修改的位置不同，不会与其他 mutation 重叠。

**模拟的真实错误**：开发者在理解 PLY 语法时，可能认为 `DIVISION unit_expression` 的语义就是"获取除号后面的单位"，而没有意识到还需要取倒数（`** -1`）才能表示"被该单位除"的语义。

### Mutation D（p_main factor*units 改为 factor/units）

**代码分析基础**：`p_main` 的 `len(p) == 3` 分支处理 `factor combined_units`，将数值因子与单位相乘（如 `10^3 * J/m/s/kpc2`）。

**选择此位置的原因**：`p_main` 是解析树的顶层节点，在调用链上游引入错误，使错误在多个测试场景下显现。同时，此位置与 `p_division_of_units` 的修改位置完全不同，避免了与其他 mutation 的重叠。

**模拟的真实错误**：开发者在处理 `factor combined_units` 时，可能误解了 CDS 格式中 factor 与 units 的关系，认为 factor 是"分母"（如 `10^3` 表示"以 10^3 为单位"，即除以 `10^3`），而非"倍数"。

### Mutation E（p_sign 默认值 1.0 改为 -1.0）

**代码分析基础**：`p_sign` 的空产生式为所有无显式符号的幂次提供默认正号 `1.0`。这是一个底层辅助函数，被 `p_numeric_power` 调用，进而影响所有带幂次的单位。

**选择此位置的原因**：`p_sign` 在调用链的最底层，其错误会通过 `p_numeric_power → p_unit_with_power → p_unit_expression → p_combined_units` 逐层传播，最终影响所有含无符号幂次的单位解析。这是典型的"上游错误，下游表现"的多层传播变异。

**模拟的真实错误**：开发者在实现 `p_sign` 的空规则时，可能受到"符号默认应该是负的"的错误直觉影响（因为在某些上下文中，省略符号可能暗示负值），或者在调试负幂次问题时不小心将 `1.0` 改为 `-1.0`。
