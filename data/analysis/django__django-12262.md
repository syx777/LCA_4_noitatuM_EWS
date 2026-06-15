# django__django-12262

## 问题背景

自定义模板标签在使用仅关键字参数（keyword-only argument）且带有默认值时，调用时提供该参数会抛出 `TemplateSyntaxError: received unexpected keyword argument`。

例如：
```python
@register.simple_tag
def my_tag(*, kwarg=42):
    return kwarg
```
在模板中调用 `{% my_tag kwarg=37 %}` 会报错，而正确行为应该是返回 37。

## Golden Patch 语义分析

`parse_bits` 中，`unhandled_kwargs` 是所有**没有默认值**的 kwonly 参数列表：

```python
unhandled_kwargs = [
    kwarg for kwarg in kwonly
    if not kwonly_defaults or kwarg not in kwonly_defaults
]
```

Bug 在第 264 行的检查：
```python
# 旧（bug）:
if param not in params and param not in unhandled_kwargs and varkw is None:
```

当某个 kwonly 参数带有默认值时，它**不在** `unhandled_kwargs` 中，所以 `param not in unhandled_kwargs` 为 `True`，条件成立，错误地抛出"unexpected keyword argument"。

修复改为：
```python
# 新（fix）:
if param not in params and param not in kwonly and varkw is None:
```

改用 `kwonly`（所有 kwonly 参数，无论是否有默认值）来做检查，这样带有默认值的 kwonly 参数也能被正确识别。

## 调用链分析

```
Library.simple_tag(func)
    └─ dec(func)
        └─ getfullargspec → params, varargs, varkw, defaults, kwonly, kwonly_defaults
        └─ compile_func(parser, token)
            └─ parse_bits(parser, bits, params, varargs, varkw, defaults,
                          kwonly, kwonly_defaults, takes_context, name)
                └─ unhandled_kwargs 初始化（第254-257行）
                └─ for bit in bits: 循环解析（第258-299行）
                    └─ 意外关键字检查（第264行）← golden fix 在此
                    └─ 重复关键字检查（第269行）
                    └─ 记录 kwarg，从 unhandled_* 中移除（第274-283行）
                └─ 最终缺少参数检查（第304-308行）

Library.inclusion_tag(filename)(func)
    └─ 与 simple_tag 类似，compile_func 同样调用 parse_bits
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 新设计 | 新增 | mutations.jsonl 中缺失 Group A，需全新设计 |
| B | 🔴 必须替换 | 替换 | 直接还原 golden patch（把 `kwonly` 换回 `unhandled_kwargs`） |
| C | 🟡 语义浅层 | 保留 | 单行删除一个条件，N=1 语义浅层，floor(1/2)=0 不替换 |
| D | 🔴 必须替换 | 替换 | 与 C 语义相同但加了明显的 `# BUG:` 注释，不自然 |
| E | 新设计 | 新增 | mutations.jsonl 中缺失 Group E，需全新设计 |

语义浅层共 1 个（C），替换其中最弱的 floor(1/2)=0 个：无需替换。

## 各组 Mutation 分析

### Group A — 新增（缺失组）

**原 mutation**：（mutations.jsonl 中不存在 Group A）

**分类**：新设计

**设计思路**：修改 `parse_bits` 末尾的最终未处理参数检查，将 `or` 改为 `and`。模拟开发者误解"只要两者都不为空才算缺参数"的错误逻辑。位置与 golden patch 完全不同（第 304 行），修改看起来合理（Python 中 `and`/`or` 在这里都语法合法）。

**最终 mutation**：
```diff
diff --git a/django/template/library.py b/django/template/library.py
index 2f74556268..84e20d7823 100644
--- a/django/template/library.py
+++ b/django/template/library.py
@@ -301,7 +301,7 @@ def parse_bits(parser, bits, params, varargs, varkw, defaults,
         # Consider the last n params handled, where n is the
         # number of defaults.
         unhandled_params = unhandled_params[:-len(defaults)]
-    if unhandled_params or unhandled_kwargs:
+    if unhandled_params and unhandled_kwargs:
         # Some positional arguments were not supplied
         raise TemplateSyntaxError(
             "'%s' did not receive value(s) for the argument(s): %s" %
```

**变异语义**：`or` → `and` 意味着只有"同时缺少位置参数和 kwonly 参数"时才抛出错误。当仅缺少 kwonly 参数（如 `simple_keyword_only_param` 不提供 `kwarg`）时，`unhandled_params=[]` 而 `unhandled_kwargs=['kwarg']`，`[] and [...]` 为 False，不会抛出 TemplateSyntaxError。这会使"required kwonly arg not supplied"的测试失败，因为没有提前报错，实际调用函数时会得到 Python TypeError 而不是 TemplateSyntaxError。

---

### Group B — 替换

**原 mutation**：
```diff
-            if param not in params and param not in kwonly and varkw is None:
+            if param not in params and param not in unhandled_kwargs and varkw is None:
```

**分类**：🔴 必须替换

**理由**：直接逆操作 golden patch，等价于把 fix 还原成 bug，是最低质量的 mutation。

**最终 mutation**（替换为不同位置的高质量变体）：
```diff
diff --git a/django/template/library.py b/django/template/library.py
index 2f74556268..7932843026 100644
--- a/django/template/library.py
+++ b/django/template/library.py
@@ -251,10 +251,7 @@ def parse_bits(parser, bits, params, varargs, varkw, defaults,
     args = []
     kwargs = {}
     unhandled_params = list(params)
-    unhandled_kwargs = [
-        kwarg for kwarg in kwonly
-        if not kwonly_defaults or kwarg not in kwonly_defaults
-    ]
+    unhandled_kwargs = list(kwonly)
     for bit in bits:
         # First we try to extract a potential kwarg from the bit
         kwarg = token_kwargs([bit], parser)
```

**变异语义**：`unhandled_kwargs` 初始化时把所有 kwonly 参数（包括有默认值的）都纳入"待处理"列表。最终检查 `if unhandled_params or unhandled_kwargs:` 时，即使 kwonly 参数有默认值且用户没有提供，它仍然留在 `unhandled_kwargs` 中，导致抛出"did not receive value"错误。错误点在初始化阶段，与 golden patch 修改的运行时检查位置完全不同，看起来像是"懒省事地直接用 list(kwonly)"的开发者错误。

---

### Group C — 保留

**原 mutation**：
```diff
-            if param not in params and param not in kwonly and varkw is None:
+            if param not in params and varkw is None:
```

**分类**：🟡 语义浅层（保留）

**理由**：删除了 `param not in kwonly` 这一整个条件，使得所有关键字参数只要不是命名的位置参数（`params`）且没有 `**kwargs`，都会通过检查。对于 kwonly 参数来说，虽然不再报"unexpected keyword argument"，但如果参数名真的不被识别，也不会报错——这是一个不同方向的语义变化。虽然是单行改动，但修改位置恰好在 golden fix 所在行的语义相关位置，保留价值足够。

**最终 mutation**（保留原样）：
```diff
diff --git a/django/template/library.py b/django/template/library.py
index 2f74556268..99e7fd8d8f 100644
--- a/django/template/library.py
+++ b/django/template/library.py
@@ -261,7 +261,7 @@ def parse_bits(parser, bits, params, varargs, varkw, defaults,
         if kwarg:
             # The kwarg was successfully extracted
             param, value = kwarg.popitem()
-            if param not in params and param not in kwonly and varkw is None:
+            if param not in params and varkw is None:
                 # An unexpected keyword argument was supplied
                 raise TemplateSyntaxError(
                     "'%s' received unexpected keyword argument '%s'" %
```

**变异语义**：彻底移除对 kwonly 的检查。提供 kwonly 参数时不再抛出"unexpected"错误，但也不会从 `unhandled_kwargs` 中移除（因为后面的 `elif param in unhandled_kwargs` 分支依然存在）。这实际上导致 kwarg 被记录到 `kwargs` 字典中，但 `unhandled_kwargs` 不更新，最终仍会抛出"did not receive value"错误——行为不同于原 bug，也不同于 fix。

---

### Group D — 替换

**原 mutation**：
```diff
-            if param not in params and param not in kwonly and varkw is None:
+            if param not in params and varkw is None:  # BUG: removed kwonly check
```

**分类**：🔴 必须替换

**理由**：语义与 Group C 完全相同，但加了明显的 `# BUG:` 注释，在代码审查中会立即暴露，不自然。

**最终 mutation**（替换为不同位置的高质量变体）：
```diff
diff --git a/django/template/library.py b/django/template/library.py
index 2f74556268..07dcabc257 100644
--- a/django/template/library.py
+++ b/django/template/library.py
@@ -278,9 +278,6 @@ def parse_bits(parser, bits, params, varargs, varkw, defaults,
                     # If using the keyword syntax for a positional arg, then
                     # consume it.
                     unhandled_params.remove(param)
-                elif param in unhandled_kwargs:
-                    # Same for keyword-only arguments
-                    unhandled_kwargs.remove(param)
         else:
             if kwargs:
                 raise TemplateSyntaxError(
```

**变异语义**：当 kwonly 参数通过关键字语法提供时，不从 `unhandled_kwargs` 中将其移除。参数被正确记录到 `kwargs` 字典，但由于仍然残留在 `unhandled_kwargs` 中，最终的 `if unhandled_params or unhandled_kwargs:` 检查会错误地报"did not receive value"，即使参数已经被提供。模拟了开发者在保持 positional arg tracking 的同时遗漏了 kwonly arg tracking 的同步删除。

---

### Group E — 新增（缺失组）

**原 mutation**：（mutations.jsonl 中不存在 Group E）

**分类**：新设计

**设计思路**：跨函数变异，修改 `inclusion_tag` 的 `compile_func` 中对 `parse_bits` 的调用，将 `kwonly` 参数传为 `[]`（空列表）。这模拟开发者在维护 `inclusion_tag` 时，误以为 kwonly 参数不需要传递（比如在重构调用签名时粗心替换）。与 `simple_tag` 形成不对称：simple_tag 正确，inclusion_tag 静默失效。

**最终 mutation**：
```diff
diff --git a/django/template/library.py b/django/template/library.py
index 2f74556268..04a6b14500 100644
--- a/django/template/library.py
+++ b/django/template/library.py
@@ -151,7 +151,7 @@ class Library:
                 bits = token.split_contents()[1:]
                 args, kwargs = parse_bits(
                     parser, bits, params, varargs, varkw, defaults,
-                    kwonly, kwonly_defaults, takes_context, function_name,
+                    [], kwonly_defaults, takes_context, function_name,
                 )
                 return InclusionNode(
                     func, takes_context, args, kwargs, filename,
```

**变异语义**：`inclusion_tag` 的 `parse_bits` 调用接收 `kwonly=[]`，意味着 parse_bits 认为该 inclusion tag 没有任何 kwonly 参数。当模板尝试使用 `{% inclusion_keyword_only_default kwarg=37 %}` 时，`kwarg not in []` 为 True，满足"unexpected keyword argument"条件，抛出 TemplateSyntaxError。F2P 测试 `inclusion_keyword_only_default kwarg=37` 失败。simple_tag 相关测试不受影响，使得这个 bug 只在 inclusion_tag 中出现，具有较强的迷惑性。

## 新设计 Mutation 说明

### Group A（`or` → `and`）

基于对调用链末端错误处理逻辑的分析。`parse_bits` 在遍历完所有 bits 后，用 `if unhandled_params or unhandled_kwargs:` 检查是否所有必须参数都已提供。改为 `and` 模拟一种"两者都缺失才报错"的逻辑误解，这种误解在真实开发中会出现（开发者可能混淆了"必须提供所有参数"和"必须同时缺少多类参数"的语义）。该位置距离 golden fix 所在行（264）约 40 行，是完全不同的代码路径。

### Group B（`list(kwonly)` 替换 filtered comprehension）

基于对 `unhandled_kwargs` 初始化逻辑的分析。原代码通过过滤掉有默认值的 kwonly 参数来构建"必须提供的 kwonly 参数"列表。改为 `list(kwonly)` 模拟开发者"先把所有 kwonly 都加进去再说"的简化做法——在测试覆盖不全时很难发现，因为带默认值且不提供时才会暴露。

### Group D（删除 `unhandled_kwargs.remove(param)`）

基于对 kwonly 参数状态追踪逻辑的分析。`unhandled_params.remove(param)` 和 `unhandled_kwargs.remove(param)` 是对称的两个操作，分别追踪位置参数和 kwonly 参数的消耗。删除后者模拟开发者只处理了位置参数的 tracking 而遗漏了 kwonly 参数的 tracking——这种不对称的遗漏是真实代码审查中极难发现的 bug 类型，因为功能上"参数已被记录到 kwargs"看似正确，只有末尾检查才会暴露问题。

### Group E（inclusion_tag 传 `[]` 给 kwonly）

基于对 `simple_tag` 与 `inclusion_tag` 调用 `parse_bits` 的对比分析。两者的调用签名完全对称，但开发者在处理 inclusion_tag 时可能误将 kwonly 参数列表替换为空列表（比如重构时忘记同步更新），制造了 simple_tag/inclusion_tag 的不对称 bug。这类跨方法的不对称 bug 在真实项目中很常见，且在没有充分覆盖两种 tag 的测试时难以发现。
