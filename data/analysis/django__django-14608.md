# django__django-14608 Mutation 分析

## 问题背景

Django 的 `Form` 会为非字段错误（non field errors）在 `ErrorList` 上添加 `nonfield` CSS 类。
而 `FormSet` 的非表单错误（non form errors）却没有对应的 CSS 类，导致自定义 `ErrorList`
无法区分「表单字段错误 / 表单非字段错误 / 表单集非表单错误」三种渲染场景。本 issue 要求为
FormSet 的非表单错误添加 `nonform` CSS 类。

## Golden Patch 语义分析

修改文件：`django/forms/formsets.py` 的 `BaseFormSet.full_clean()`，共两处：

1. 初始化（line 336）：
   `self._non_form_errors = self.error_class()`
   → `self.error_class(error_class='nonform')`
2. 捕获 `clean()` / max / min 校验抛出的 `ValidationError` 时（line 383）：
   `self._non_form_errors = self.error_class(e.error_list)`
   → `self.error_class(e.error_list, error_class='nonform')`

`error_class` 参数最终传给 `django/forms/utils.py::ErrorList.__init__`，使
`self.error_class = 'errorlist nonform'`，从而 `as_ul()` 渲染出
`<ul class="errorlist nonform">`。

## 调用链分析

`formset.non_form_errors()` → `full_clean()` 填充的 `self._non_form_errors`
→ `ErrorList.__str__` → `as_ul()` → 输出 `class="errorlist nonform"`。

关键点：4 个 F2P 测试都是**触发 max/min/clean 校验错误**的场景，因此实际生效的赋值是
**except 分支**（line 383），而非初始化行。三个 forms_tests F2P 用例断言
`str(non_form_errors())` 等于 `<ul class="errorlist nonform">...`；admin_views 用例断言
与 `ErrorList([...], error_class='nonform')` 的字符串相等。

另一个约束：原有 P2P 断言 `non_form_errors() == ['...']` 依赖 `ErrorList.__eq__`
（只比较列表内容、忽略 CSS 类），所以 mutation 必须保留 `e.error_list` 不变，只需污染
`error_class` 字符串即可让 F2P 失败而不误伤 P2P。

## 替换决策总览

| 槽位 | 原策略 | 原内容 | 分类 | 决策 | 新策略码 |
|------|--------|--------|------|------|----------|
| B | B (boundary) | line 367 `>`→`>=` 偏移 | 🔴 无效（破坏 P2P，golden 未修复） | 替换 | B1 |
| C | C (type) | 删除 except 分支 `error_class='nonform'` | 🔴 golden 直接回退 | 替换 | C3 |
| D | D (I/O) | 删除该行（带尾逗号），与 C 等价 | 🔴 与 C 功能等价冗余 | 替换 | D1 |
| E | E (test-exp) | `'nonform'`→`'nonfield'` | 🟢 自然单词替换，有效 | 保留 | E1 |

## 各组 Mutation 分析

- **槽位 B（原）**：把 `> self.max_num` 改成 `>= self.max_num`。该行是 max 校验边界，
  与 golden patch 完全无关。验证显示它破坏了 P2P 用例
  `test_validate_max_ignores_forms_marked_for_deletion`（该用例 golden 后应通过），
  属于「golden 不能修复」的无效 mutation，必须替换。
- **槽位 C（原）/ D（原）**：两者都是把 golden 新增的 `error_class='nonform'` 参数删掉，
  即直接回退 golden patch，且彼此功能等价（仅尾逗号差异）。属冗余，必须替换。
- **槽位 E（原）**：`'nonform'`→`'nonfield'`。是一个非常自然的「拷贝了 Form 的
  nonfield 命名」的开发者笔误，渲染出 `errorlist nonfield` 与期望 `errorlist nonform`
  不符，破坏全部字符串断言。有效且自然，保留。

## 新设计 Mutation 说明

为最大化失败模式正交性，三个替换分别采用「条件依赖错误数量 / 字符串空白污染 / 条件依赖配置状态」三类不同机理：

- **槽位 B → B1（边界/计数偏移）**：
  `error_class='nonform' if len(e.error_list) > 1 else None`。
  开发者误以为「只有多条错误才需要分组 CSS 类」，对错误条数做了 off-by-one 式的边界判断。
  本 issue 的 3 个 F2P 场景都恰好只有 1 条非表单错误，落入 `else None` 分支，渲染成
  `errorlist`（无 nonform），断言失败。正交点：若某测试构造 ≥2 条非表单错误则会侥幸通过，
  弱测试套件难以覆盖该边界。
- **槽位 C → C3（文本/编码）**：`error_class='nonform '`（尾部多一个空格）。
  `ErrorList` 拼成 `'errorlist nonform '`，渲染 `class="errorlist nonform "`，与期望多一个
  尾随空格。极隐蔽的空白字符差异，肉眼 review 与宽松断言均易漏过，但精确字符串比较必失败。
- **槽位 D → D1（状态/配置依赖）**：
  `error_class='nonform' if self.can_delete else None`。
  开发者误把 CSS 分组类与 `can_delete` 配置绑定。F2P 用例的 FormSet 均未开启
  `can_delete`，故走 `None` 分支，丢失 nonform 类而失败。正交点：仅在 `can_delete=False`
  的配置下暴露，依赖 formset 配置状态，与计数/空白两类失败机理完全不同。
- **槽位 E → E1（断言期望/字面量）**：保留原 `'nonfield'`，自然的命名混淆笔误。

四组失败机理互相正交：条数边界(B) / 空白字符(C) / 配置状态(D) / 错误字面量(E)。
全部经真实 `forms_tests.tests.test_formsets` 运行验证：py_compile 通过且各自触发 F2P 失败，
无 P2P 误伤。
