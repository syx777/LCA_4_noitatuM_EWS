# django__django-11477

## 问题背景

当使用 `translate_url()` 翻译含有**可选命名分组**的 URL 时，`RegexPattern.match()` 返回的 `kwargs` 中包含值为 `None` 的键（对应未匹配的可选分组）。这些 `None` 值随后被传入 `reverse()`，导致 `reverse()` 无法找到匹配的 URL 模式，翻译失败。

例如，对于模式 `r'^optional/(?P<arg1>\d+)/(?:(?P<arg2>\d+)/)?'`，当 URL 为 `/optional/1/` 时，`match.groupdict()` 返回 `{'arg1': '1', 'arg2': None}`。golden patch 通过过滤掉 `None` 值修复了此问题。

## Golden Patch 语义分析

Golden patch 仅修改 `RegexPattern.match()` 中的一行：

```python
# Before (base commit — bug):
kwargs = match.groupdict()

# After (golden patch — fix):
kwargs = {k: v for k, v in match.groupdict().items() if v is not None}
```

核心语义：正则表达式的 `groupdict()` 方法对于未匹配的可选命名分组会返回 `None`，而 Django URL 解析框架在将 `kwargs` 传给 `reverse()` 时期望 `kwargs` 中只包含实际匹配到的值。`reverse()` 的 `_reverse_with_prefix()` 方法使用 `set(kwargs).symmetric_difference(params)` 来匹配 URL 模式，若 kwargs 中含 `None` 键则会破坏参数匹配逻辑。

## 调用链分析

```
translate_url(url, lang_code)          [django/urls/base.py]
  → resolve(parsed.path)               [→ URLResolver.resolve()]
  → match = ResolverMatch(...)         [包含 kwargs]
  → reverse(to_be_reversed, kwargs=match.kwargs)
      → URLResolver._reverse_with_prefix()
          → set(kwargs).symmetric_difference(params)  ← None 值在此导致参数不匹配

被修改函数上游：
  URLPattern.resolve()
    → self.pattern.match(path)        ← RegexPattern.match() 在此返回含 None 的 kwargs
    → ResolverMatch(callback, args, kwargs, ...)

被修改函数下游：
  RegexPattern.match() 返回 (new_path, args, kwargs)
  → URLPattern.resolve() 将 kwargs 合并 default_args
  → URLResolver.resolve() 将 kwargs 传播到 sub_match_dict
  → translate_url() 用 kwargs 调用 reverse()
```

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | diff 等同于直接还原 golden patch（`match.groupdict()`），是 base commit 原始 bug |
| B | 🔴 必须替换 | 替换 | 仅去掉 `if v is not None` 条件但保留字典推导式，功能等价于直接还原 |
| C | 缺失 | 新建 | mutations.jsonl 中 Group C 不存在，需要设计 |
| D | 🔴 必须替换 | 替换 | 与 Group A 完全相同的 diff（`match.groupdict()`），冗余 |
| E | 🔴 必须替换 | 替换 | `if v is None` 条件明显不自然（只保留 None 值），代码审查中立即可见 |

语义浅层共 0 个，必须替换 4 个（A/B/D/E），新建 1 个（C）。

## 各组 Mutation 分析

### Group A — 替换

**原 mutation**：
```diff
-            kwargs = {k: v for k, v in match.groupdict().items() if v is not None}
+            kwargs = match.groupdict()
```
**分类**：🔴 必须替换
**理由**：这正是 base commit 的原始代码，是 golden patch 的直接逆操作。测试套件生成工具可以直接检测到这是 patch 的反转。

**最终 mutation**：
```diff
diff --git a/django/urls/resolvers.py b/django/urls/resolvers.py
index 247e3680c0..d6dccd85af 100644
--- a/django/urls/resolvers.py
+++ b/django/urls/resolvers.py
@@ -346,7 +346,7 @@ class URLPattern:
         if match:
             new_path, args, kwargs = match
             # Pass any extra_kwargs as **kwargs.
-            kwargs.update(self.default_args)
+            kwargs = {**kwargs, **self.default_args}
             return ResolverMatch(self.callback, args, kwargs, self.pattern.name, route=str(self.pattern))
 
     @cached_property
```
**变异语义**：将 `URLPattern.resolve()` 中 `default_args` 的合并方式从 `update()`（match 优先）改为字典解包（`default_args` 优先）。在没有 `default_args` 重叠的典型测试中行为一致；只有当 URL 模式配置了 `default_args`，且实际匹配值与默认值不同时才会暴露 bug（`default_args` 的值会错误覆盖实际匹配结果）。代码外观上合法，是常见的合并策略误用。

---

### Group B — 替换

**原 mutation**：
```diff
-            kwargs = {k: v for k, v in match.groupdict().items() if v is not None}
+            kwargs = {k: v for k, v in match.groupdict().items() }
```
**分类**：🔴 必须替换
**理由**：语义上等同于直接还原 golden patch，仅保留了字典推导式外壳但去掉了核心过滤条件，功能与 `match.groupdict()` 完全相同。

**最终 mutation**：
```diff
diff --git a/django/urls/resolvers.py b/django/urls/resolvers.py
index 247e3680c0..2217a6a116 100644
--- a/django/urls/resolvers.py
+++ b/django/urls/resolvers.py
@@ -154,7 +154,7 @@ class RegexPattern(CheckURLMixin):
             # non-named groups. Otherwise, pass all non-named arguments as
             # positional arguments.
             kwargs = {k: v for k, v in match.groupdict().items() if v is not None}
-            args = () if kwargs else match.groups()
+            args = match.groups() if kwargs else ()
             return path[match.end():], args, kwargs
         return None
```
**变异语义**：将 `RegexPattern.match()` 中 args/kwargs 的互斥逻辑反转。原逻辑：有 kwargs 时 args 为空（忽略非命名分组），无 kwargs 时 args 为非命名分组。变异后：有 kwargs 时 args 为非命名分组（同时有 kwargs 和 args），无 kwargs 时 args 为空。这会在含命名分组的 URL 模式中意外包含位置参数，导致 `reverse()` 调用时参数数量冲突（`mixed_args` 等场景）；对无命名分组的纯位置参数 URL 则完全返回空 args，使 view 收不到任何参数。

---

### Group C — 新建

**最终 mutation**：
```diff
diff --git a/django/urls/base.py b/django/urls/base.py
index 0e1c3d909c..da4cd0d6df 100644
--- a/django/urls/base.py
+++ b/django/urls/base.py
@@ -169,7 +169,7 @@ def translate_url(url, lang_code):
     except Resolver404:
         pass
     else:
-        to_be_reversed = "%s:%s" % (match.namespace, match.url_name) if match.namespace else match.url_name
+        to_be_reversed = "%s:%s" % (match.app_name, match.url_name) if match.app_name else match.url_name
         with override(lang_code):
             try:
                 url = reverse(to_be_reversed, args=match.args, kwargs=match.kwargs)
```
**分类**：🟢 新建高质量 mutation
**变异语义**：`translate_url()` 在构建待反向解析的 URL 名时，错误地用 `app_name`（应用名称）替代 `namespace`（实例名称空间）。在没有命名空间或 `app_name == namespace` 的简单情况下，两者相同，测试通过。但在使用实例命名空间的场景（如 `test-ns1` 命名空间属于 `testapp` 应用）中，`namespace='test-ns1'` 而 `app_name='testapp'`，使用 `app_name` 会导致 `NoReverseMatch`，`translate_url` 静默返回原 URL 而非翻译后的 URL。这是真实开发者会犯的错误——混淆 `app_name` 和 `namespace` 的概念。

---

### Group D — 替换

**原 mutation**：
```diff
-            kwargs = {k: v for k, v in match.groupdict().items() if v is not None}
+            kwargs = match.groupdict()
```
**分类**：🔴 必须替换（与 Group A 完全相同，冗余）

**最终 mutation**：
```diff
diff --git a/django/urls/resolvers.py b/django/urls/resolvers.py
index 247e3680c0..e0196ae02c 100644
--- a/django/urls/resolvers.py
+++ b/django/urls/resolvers.py
@@ -550,9 +550,9 @@ class URLResolver:
                         sub_match_dict.update(sub_match.kwargs)
                         # If there are *any* named groups, ignore all non-named groups.
                         # Otherwise, pass all non-named arguments as positional arguments.
-                        sub_match_args = sub_match.args
-                        if not sub_match_dict:
-                            sub_match_args = args + sub_match.args
+                        sub_match_args = args + sub_match.args
+                        if sub_match_dict:
+                            sub_match_args = sub_match.args
                         current_route = '' if isinstance(pattern, URLPattern) else str(pattern.pattern)
                         return ResolverMatch(
                             sub_match.func,
```
**变异语义**：在 `URLResolver.resolve()` 合并父/子 URL 解析器匹配结果时，反转了位置参数的聚合逻辑。原逻辑：默认只用子匹配的 args，仅当没有 kwargs 时才拼接父级 args。变异后：默认总是拼接父级 args，仅当有 kwargs 时才只用子匹配 args。这导致在有命名分组的 included URL 解析中，父级 URL 段的位置参数被错误地包含在最终 args 中（如 `/included/12/no_kwargs/42/37/` 中的 `12` 在有 kwargs 时不应再出现）。

---

### Group E — 替换

**原 mutation**：
```diff
-            kwargs = {k: v for k, v in match.groupdict().items() if v is not None}
+            kwargs = {k: v for k, v in match.groupdict().items() if v is None}
```
**分类**：🔴 必须替换（`if v is None` 明显不自然，保留 None 值、丢弃有效值）

**最终 mutation**：
```diff
diff --git a/django/urls/resolvers.py b/django/urls/resolvers.py
index 247e3680c0..a6379a02bc 100644
--- a/django/urls/resolvers.py
+++ b/django/urls/resolvers.py
@@ -617,7 +617,7 @@ class URLResolver:
                         continue
                     candidate_subs = dict(zip(params, args))
                 else:
-                    if set(kwargs).symmetric_difference(params).difference(defaults):
+                    if set(kwargs).difference(params).difference(defaults):
                         continue
                     if any(kwargs.get(k, v) != v for k, v in defaults.items()):
                         continue
```
**变异语义**：在 `_reverse_with_prefix()` 中，将双向对称差集（`symmetric_difference`）改为单向差集（`difference`）。原逻辑：若 kwargs 的键集合与 URL 参数集合不一致（多了或少了），则跳过该 URL 模式候选。变异后：只检查 kwargs 中有而 URL 参数没有的多余键，但不检查 URL 参数有而 kwargs 没有的缺失键。这导致 `reverse()` 在 kwargs 缺少必要参数时仍然尝试匹配，会选出不匹配的候选并在格式化替换时出错，或在更复杂的场景中返回错误的 URL。对于不含可选参数的典型测试，kwargs 与 params 完全匹配，`difference()` 与 `symmetric_difference()` 结果相同，测试通过；只有在 kwargs 提供的参数数量不完整时才暴露。

---

## 新设计 Mutation 说明

### Group A（替换 URLPattern.resolve() 中的 default_args 合并）

分析：`URLPattern.resolve()` 在获得 `RegexPattern.match()` 的结果后，用 `kwargs.update(self.default_args)` 将视图的默认参数合并到匹配的 kwargs 中。这里的语义契约是：URL 匹配到的值**优先**于 `default_args`（即 `update()` 时匹配值不会被覆盖）。将其改为字典解包 `{**kwargs, **self.default_args}` 看似等价，实则颠倒了优先级——`default_args` 会覆盖实际匹配值。这模拟了开发者在合并两个字典时对操作符优先级的误解。

### Group B（反转 RegexPattern.match() 中 args 的条件逻辑）

分析：`args = () if kwargs else match.groups()` 是 Django URL 解析的关键规则：一旦有命名分组（kwargs 非空），就丢弃所有位置参数，避免双重传参。反转此条件直接违反了这一不变式，在 mixed_args 测试等场景下立即失败，但对纯命名参数（`args` 恒为 `()`）或纯位置参数（kwargs 恒为 `{}`）的 URL 模式无影响，能通过大量简单测试。

### Group C（translate_url 中 namespace vs app_name 混用）

分析：`ResolverMatch` 同时有 `namespace`（实例名称空间，如 `test-ns1`）和 `app_name`（应用名称，如 `testapp`）两个属性，两者在单实例无命名空间时相同，但在多实例部署时不同。用 `app_name` 替代 `namespace` 模拟了真实开发者对这两个概念混淆的错误，对简单场景透明，只在有命名空间的 URL 翻译（如 `test_translate_url_utility` 测试的命名空间部分）下失败。

### Group D（URLResolver.resolve() 中 sub_match_args 聚合逻辑反转）

分析：included URL conf 解析时，父级 URLResolver 和子级 URLPattern 各自匹配一段 URL，子匹配的 args 和父级 args 的合并规则是：有 kwargs 时只用子匹配 args（因为命名参数已涵盖所有信息），无 kwargs 时才拼接父级 args（确保位置参数完整）。反转此逻辑导致在有 kwargs 的 included URL 中意外包含父级位置参数，影响 `resolve_test_data` 中 `/included/12/no_kwargs/42/37/` 类型的测试。

### Group E（_reverse_with_prefix 中 symmetric_difference → difference）

分析：`_reverse_with_prefix()` 通过 `set(kwargs).symmetric_difference(params)` 确保 kwargs 的键集合与 URL 模式所需参数集合完全一致（考虑 defaults 豁免）。改为 `difference()` 只检查"多余的 kwargs"而不检查"缺失的参数"，导致提供不完整参数时 `reverse()` 不能正确跳过候选模式。这模拟了开发者对集合差运算语义的错误选择——常见的集合操作混用。
