# django__django-9296

## 问题背景

Django 的 `Paginator` 类缺少 `__iter__` 方法，用户无法直接用 `for page in paginator:` 或 `iter(paginator)` 来遍历所有页面。目前必须手动通过 `paginator.page_range` 再逐一调用 `paginator.page(n)` 来访问每页。golden patch 为 `Paginator` 类添加了标准的 `__iter__` 方法，使其可以直接迭代，每次 yield 一个 `Page` 对象。

## Golden Patch 语义分析

Golden patch 在 `Paginator.__init__` 之后插入：

```python
def __iter__(self):
    for page_number in self.page_range:
        yield self.page(page_number)
```

核心语义：`__iter__` 委托给 `page_range`（产生 1 到 num_pages 的整数范围），然后对每个页码调用 `self.page(page_number)` 返回对应的 `Page` 对象。正确性依赖于：
1. `page_range` 返回完整的 `range(1, num_pages + 1)`；
2. `page(number)` 正确计算每页的切片范围（`bottom` 和 `top`）；
3. 每个 `Page` 对象正确封装了对应的数据子集。

## 调用链分析

```
Paginator.__iter__()
  └─ self.page_range  →  range(1, self.num_pages + 1)
       └─ self.num_pages  →  ceil(hits / per_page)
            └─ self.count  →  len(object_list) 或 object_list.count()
  └─ self.page(page_number)
       └─ self.validate_number(number)
       └─ bottom = (number - 1) * per_page
       └─ top = bottom + per_page
       └─ if top + orphans >= count: top = count   ← 孤儿项合并逻辑
       └─ self._get_page(object_list[bottom:top], number, self)
            └─ Page(object_list_slice, number, paginator)
                 └─ Page.end_index()  ← 依赖孤儿项特殊处理
```

上游调用者：用户代码通过 `for page in paginator:` 或 `list(paginator)` 触发。
下游依赖：`Page` 对象的 `end_index()`、`start_index()`、`has_next()` 等方法在迭代结果上被使用。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 必须替换 | 替换 | `yield page_number` 等同于 `__iter__` 不起作用，是 golden fix 的直接逆操作 |
| B | 语义浅层 | 保留 | `page_range` 少一页，在关键控制流上，模拟真实 off-by-one 错误 |
| C | 必须替换 | 替换 | `page_number + ""` 立即抛 TypeError，明显人工痕迹，非自然错误 |
| D | 语义浅层 | 替换 | `list(page_range)[1:]` 跳过第一页，与 B 类型相似且更容易被测试直接捕获 |
| E | 必须替换 | 替换 | 添加 `iter_pages=False` 标志，功能等价冗余且代码风格不自然 |

语义浅层共 2 个（B、D），替换其中最弱的 floor(2/2) = 1 个：**D**

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
-            yield self.page(page_number)
+            yield page_number
```
**分类**：🔴 必须替换

**理由**：这是 golden patch 的直接逆操作——golden patch 添加了 `yield self.page(page_number)` 的 `__iter__`，此 mutation 将其改为 `yield page_number`，等于把 `__iter__` 变成了 `page_range` 的复制，完全破坏了功能。`test_paginator_iteration` 立即能检测（它期望 `list(next(page_iterator))` 返回数据项，而不是页码整数）。

**最终 mutation**（替换）：
```diff
diff --git a/django/core/paginator.py b/django/core/paginator.py
index ec0ebe0403..e9eb136917 100644
--- a/django/core/paginator.py
+++ b/django/core/paginator.py
@@ -73,7 +73,7 @@ class Paginator:
         number = self.validate_number(number)
         bottom = (number - 1) * self.per_page
         top = bottom + self.per_page
-        if top + self.orphans >= self.count:
+        if top + self.orphans > self.count:
             top = self.count
         return self._get_page(self.object_list[bottom:top], number, self)
```

**变异语义**：`page()` 计算页面切片时，当 `top + orphans == count`（恰好触发孤儿项合并的边界情况），`>=` 改为 `>` 使合并条件失效。结果：最后一页不包含孤儿项，孤儿项被丢失，迭代器产生的最后一个 Page 对象数据不完整。对于典型的无孤儿场景（`orphans=0`）所有测试通过，只在设置了 `orphans > 0` 且最后一页恰好触发合并时失败。代码看起来完全合理，只是把"大于等于"改成了"大于"。

---

### Group B — 保留

**原 mutation**：
```diff
-        return range(1, self.num_pages + 1)
+        return range(1, self.num_pages)
```
**分类**：🟡 语义浅层（保留）

**理由**：修改位置在 `page_range` 属性，这是 `__iter__` 的直接依赖，也被模板循环等广泛使用。单个 `+1` 的缺失导致最后一页被跳过，能模拟真实的 off-by-one 编程错误。`test_paginator_iteration` 会失败（最后一页 `[3]` 不被 yield），而其他只检查典型中间页的测试会通过。

**最终 mutation**（保留原样）：
```diff
diff --git a/django/core/paginator.py b/django/core/paginator.py
index ec0ebe0403..261bed81fc 100644
--- a/django/core/paginator.py
+++ b/django/core/paginator.py
@@ -108,7 +108,7 @@ class Paginator:
         Return a 1-based range of pages for iterating through within
         a template for loop.
         """
-        return range(1, self.num_pages + 1)
+        return range(1, self.num_pages)

     def _check_object_list_is_ordered(self):
```

**变异语义**：`page_range` 范围少了最后一页，导致 `__iter__` 跳过最后一页。只有单页数据（`num_pages=1`）时，连第一页也不会迭代（`range(1,1)` 为空）。多数对页范围做宽泛检查的测试会通过，只有明确验证最后一页存在的测试（如 `test_paginator_iteration`）会失败。

---

### Group C — 替换

**原 mutation**：
```diff
-            yield self.page(page_number)
+            yield self.page(page_number + "")
```
**分类**：🔴 必须替换

**理由**：`page_number + ""` 在 Python 中对 int 和 str 执行加法，立即抛出 `TypeError: unsupported operand type(s) for +: 'int' and 'str'`。这是明显的人工痕迹——没有真实开发者会犯这种错误。任何调用 `iter(paginator)` 的测试都会立即崩溃。

**最终 mutation**（替换）：
```diff
diff --git a/django/core/paginator.py b/django/core/paginator.py
index ec0ebe0403..325fb90f39 100644
--- a/django/core/paginator.py
+++ b/django/core/paginator.py
@@ -71,7 +71,7 @@ class Paginator:
     def page(self, number):
         """Return a Page object for the given 1-based page number."""
         number = self.validate_number(number)
-        bottom = (number - 1) * self.per_page
+        bottom = number * self.per_page
         top = bottom + self.per_page
         if top + self.orphans >= self.count:
             top = self.count
```

**变异语义**：`page()` 计算底部索引时，`(number - 1) * per_page` 改为 `number * per_page`，使每页的数据切片偏移一页。第 1 页返回第 2 页的数据，第 2 页返回第 3 页的数据，最后一页的切片越界（返回空列表）。语法完全合法，逻辑看起来也"差不多"，但迭代器产生的每个 Page 对象内容都错位了。`test_paginator_iteration` 期望第 1 页含 `[1, 2]`，但实际得到 `[3]`，会失败。

---

### Group D — 替换

**原 mutation**：
```diff
-        for page_number in self.page_range:
+        for page_number in list(self.page_range)[1:]:
```
**分类**：🟡 语义浅层（替换）

**理由**：与 Group B（`page_range` 少最后一页）类型相似，但更简单——直接跳过 index 0（即第一页 page_number=1）。`test_paginator_iteration` 从第一页开始检查，会立即失败。与 B 相比，D 的修改位置（`__iter__` 内部）更孤立，且效果（跳过第一页）比 B（跳过最后一页）更容易被浅层测试捕获，选为替换。

**最终 mutation**（替换）：
```diff
diff --git a/django/core/paginator.py b/django/core/paginator.py
index ec0ebe0403..a08d220b2c 100644
--- a/django/core/paginator.py
+++ b/django/core/paginator.py
@@ -100,7 +100,7 @@ class Paginator:
         if self.count == 0 and not self.allow_empty_first_page:
             return 0
         hits = max(1, self.count - self.orphans)
-        return ceil(hits / self.per_page)
+        return hits // self.per_page

     @property
     def page_range(self):
```

**变异语义**：`num_pages` 的计算由 `ceil(hits / per_page)` 改为整数除法 `hits // per_page`，当数据总量不能被 `per_page` 整除时（即有不满页的最后一页），`num_pages` 少 1，导致 `page_range` 少一页，`__iter__` 不会产生最后一页。对于整除情况（如 4 个项目每页 2 个）完全透明，只在有尾部数据的常见情况下失败。`test_paginator_iteration` 使用 3 个项目每页 2 个（需要 2 页），`ceil(2/2)=2`，但 `2//2=1`，只产生 1 页，测试会失败。

---

### Group E — 替换

**原 mutation**：
```diff
+                 allow_empty_first_page=True, iter_pages=False):
+        self.iter_pages = iter_pages
+    def __iter__(self):
+        for page_number in self.page_range:
+            if self.iter_pages:
+                yield self.page(page_number)
+            else:
+                yield page_number
```
**分类**：🔴 必须替换

**理由**：添加 `iter_pages=False` 标志，默认行为等价于 `yield page_number`（即 Group A 的 bug），只有传入 `iter_pages=True` 才正常。这是**功能等价冗余**（默认行为等同于直接还原），且**不自然**（添加一个改变 `__iter__` 默认行为的标志与 Python 协议相违背，代码审查中会立即被质疑）。

**最终 mutation**（替换）：
```diff
diff --git a/django/core/paginator.py b/django/core/paginator.py
index ec0ebe0403..a093830155 100644
--- a/django/core/paginator.py
+++ b/django/core/paginator.py
@@ -184,7 +184,4 @@ class Page(collections.abc.Sequence):
         Return the 1-based index of the last object on this page,
         relative to total objects found (hits).
         """
-        # Special case for the last page because there can be orphans.
-        if self.number == self.paginator.num_pages:
-            return self.paginator.count
         return self.number * self.paginator.per_page
```

**变异语义**：`Page.end_index()` 移除了最后一页的孤儿项特殊处理，对所有页都统一返回 `number * per_page`。对于最后一页有孤儿项的情况，`end_index()` 返回值比实际更大（声称最后一页包含更多项目）。`__iter__` 本身正常运行，每个 Page 对象的数据是正确的；但调用 `page.end_index()` 时最后一页返回错误的索引。修改位置远离 `__iter__`，代码看起来完全合理（"去掉特殊情况"），只有使用 `end_index()` 的测试在有孤儿项时失败。

## 新设计 Mutation 说明

### Group A 替换设计
基于对 `page()` 方法孤儿项合并逻辑的深入分析。`if top + self.orphans >= self.count` 是一个精心设计的边界条件：当剩余项目数量 ≤ orphans 时，将其合并到当前页。`>=` 改为 `>` 只影响 `top + orphans == count` 的精确边界，即数据量恰好整除 per_page 且 orphans > 0 的情况。此错误模拟了开发者对"是否包含等于情况"的判断失误，是真实常见的边界条件 bug。

### Group C 替换设计
基于对 `page()` 函数索引计算的分析。`bottom = (number - 1) * per_page` 是标准的 0-based 偏移计算，`number - 1` 将 1-based 页码转为 0-based。去掉 `-1` 变为 `number * per_page` 模拟开发者忘记 1-based 到 0-based 转换的错误——这是真实开发中的常见混淆，尤其对熟悉 0-based 索引的开发者。结果是每页的内容偏移一整页，表现为数据错乱而非崩溃。

### Group D 替换设计
基于对 `num_pages` 计算公式的分析。`ceil(hits / per_page)` 使用浮点除法加天花板函数确保最后一个不满页被计入。用整数除法 `//` 替代模拟开发者选错除法运算符的错误（Python 3 中 `/` 和 `//` 行为不同，经常是混淆来源）。此错误影响 `num_pages` → `page_range` → `__iter__` 的整个链路，展现跨函数错误传播效果，且只在数据量不整除时出现。

### Group E 替换设计
基于对 `Page` 对象契约的分析。`end_index()` 的特殊处理是 `Page` 接口的语义契约：调用者期望得到该页实际包含的最后一个对象的正确 1-based 索引。移除最后一页特殊处理模拟开发者认为"统一公式就够了"的判断失误。bug 存在于 `Page` 类中，与 `__iter__` 的实现分离，在调用链上更深处才显现，代码审查时不容易关联到 iteration 功能。
