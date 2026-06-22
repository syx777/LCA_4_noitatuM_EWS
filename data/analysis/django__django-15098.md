# django__django-15098

## 问题背景

i18n 的 URL 语言前缀不支持同时含 script 与 region 的 locale（如 `en-latn-us`、`en-Latn-US`）。`i18n_patterns` 用 `language_code_prefix_re` 从路径里提取语言码，旧正则 `^/(\w+([@-]\w+)?)(/|$)` 只允许一个 `-`/`@` 子标签，因此 `/en-latn-us/` 匹配失败返回 404。Golden patch 把子标签重复次数从 `?`（0 或 1）改为 `{0,2}`（0 到 2），从而支持 `lang-script-region` 三段式。

## Golden Patch 语义分析

```python
-language_code_prefix_re = _lazy_re_compile(r'^/(\w+([@-]\w+)?)(/|$)')
+language_code_prefix_re = _lazy_re_compile(r'^/(\w+([@-]\w+){0,2})(/|$)')
```
核心语义：**允许路径语言前缀含最多两个 `-`/`@` 分隔的子标签**，即 `en`、`en-us`、`en-latn-us` 都能被捕获组 1 提取出来。提取到的 `lang_code` 再交给 `get_supported_language_variant` 做大小写无关的匹配（`language_code_re` 带 `re.IGNORECASE`，故 `en-Latn-US` 也能识别）。修复同时依赖：(1) 前缀正则的子标签上限、(2) `get_supported_language_variant` 的匹配逻辑、(3) `language_code_re` 的大小写无关、(4) 设置变更时清理各类 lru_cache。

F2P 测试 `test_get_language_from_path_real`（多种 path→language 映射，含 `en-latn-us`、`en-Latn-US`、`de-ch-1901`、`nan-hani-tw` 等三段式）与 `test_page_with_dash`（`/de-simple-page-test/` 这种长 dash 路径不应被误当作语言码）。

## 调用链分析

`get_language_from_path(path)` → `language_code_prefix_re.match(path)` 提取 `lang_code` → `get_supported_language_variant(lang_code, strict)`，后者构造 `possible_lang_codes`（含逐级回退），遍历 `if code in supported_lang_codes and check_for_language(code): return code`，再做 `generic_lang_code + '-'` 前缀回退。`reset_cache`（监听 `setting_changed`）在 LANGUAGES/LANGUAGE_CODE 变更时清理 `check_for_language`、`get_languages`、`get_supported_language_variant` 三个缓存——测试用 `@override_settings` 改 LANGUAGES，依赖该清理生效。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🔴 必须替换 | 替换 | 原 `{0,2}`→`{0,1}`，直接还原旧正则；与 E 功能等价（都退回单子标签） |
| B | 🟢 高质量 | 保留 | `and`→`or` 改变匹配逻辑，使未受支持的语言码被错误接受，跨多 path 微妙失败 |
| C | 🟢 高质量 | 保留 | 删除 `re.IGNORECASE`，仅 BCP-47 大写形式 `en-Latn-US` 失败，极隐蔽 |
| D | 🟢 高质量 | 保留 | 注释掉 `get_languages.cache_clear()`，缓存陈旧导致 override_settings 后语言列表不更新 |
| E | 🔴 必须替换 | 替换 | 原 `{0,2}`→`?`，即旧正则本身，与 A 等价的直接还原 |

语义浅层 0 个；A、E 为必须替换（直接还原 + 互相等价），B/C/D 为高质量保留。A、E 替换为方向相反的 off-by-one 边界错误，与"还原"区分开。

## 各组 Mutation 分析

### Group A — 替换（B1 off-by-one 下界）
**原 mutation**：`{0,2}`→`{0,1}`（= 旧正则，直接还原）。
**最终 mutation**：
```diff
-language_code_prefix_re = _lazy_re_compile(r'^/(\w+([@-]\w+){0,2})(/|$)')
+language_code_prefix_re = _lazy_re_compile(r'^/(\w+([@-]\w+){1,2})(/|$)')
```
**变异语义**：把子标签重复区间从 `{0,2}` 改成 `{1,2}`，即**要求至少一个** `-`/`@` 子标签。三段式 `en-latn-us`（2 个子标签）仍匹配，但**无子标签的纯语言码** `/pl/`、`/en/`、`/de/` 因 `{1,2}` 下界为 1 而匹配失败 → 返回 None。模拟"locale 至少要有 region"的边界误解，是经典的 off-by-one（下界从 0 误成 1）。与原"直接还原成 `?`"完全不同。

### Group B — 保留（B3 布尔逻辑反转）
```diff
-            if code in supported_lang_codes and check_for_language(code):
+            if code in supported_lang_codes or check_for_language(code):
```
**变异语义**：把"既在支持列表中、又能加载该语言"的合取改成析取。于是只要 `check_for_language(code)` 为真（该语言有翻译文件）即返回，即便它**不在** LANGUAGES 配置里。导致 `/en-gb/` 这类应回退到 `en` 的输入被错误地直接返回 `en-gb` 等。跨多个 path 子断言微妙失败，保留。

### Group C — 保留（C-大小写/数据形状）
```diff
 language_code_re = _lazy_re_compile(
-    r'^[a-z]{1,8}(?:-[a-z0-9]{1,8})*(?:@[a-z0-9]{1,20})?$',
-    re.IGNORECASE
+    r'^[a-z]{1,8}(?:-[a-z0-9]{1,8})*(?:@[a-z0-9]{1,20})?$'
 )
```
**变异语义**：删除 `re.IGNORECASE`，使语言码校验变为大小写敏感。小写形式 `en-latn-us` 仍通过，但 **BCP-47 规范大写** `en-Latn-US` 校验失败 → 回退成 `en`，断言 `g('/en-Latn-US/')=='en-Latn-US'` 失败。极隐蔽：只在大写变体下暴露。保留。

### Group D — 保留（D1 状态/缓存重置不完整）
```diff
         check_for_language.cache_clear()
-        get_languages.cache_clear()
+        # get_languages.cache_clear()
         get_supported_language_variant.cache_clear()
```
**变异语义**：注释掉三处缓存清理中的一处（`get_languages`）。`@override_settings(LANGUAGES=...)` 改了配置，但 `get_languages` 的 lru_cache 仍返回旧语言列表，导致新加的 `en-latn-us` 等不被认作支持语言。多步（先设置、再查询）序列下才暴露的状态 bug。保留。

## 新设计 Mutation 说明

A、E 原本是同一修复的两种"直接还原"写法（`{0,1}` 与 `?` 等价），属冗余。替换为**方向相反的两个 off-by-one 边界错误**：A 把下界从 0 抬到 1（纯语言码失配），E 把上界从 2 抬到 3（见下）。两者都保留了"看似合理的量词调整"，但破坏不同输入集，且都不等于还原 golden。B/C/D 本就是作用于不同函数/不同机制（匹配逻辑、大小写、缓存）的高质量变异，予以保留。全部经实测：golden 通过、变异令 F2P 失败、`base→golden→test_patch` 后干净应用。

### Group E — 替换（B1 off-by-one 上界）
```diff
-language_code_prefix_re = _lazy_re_compile(r'^/(\w+([@-]\w+){0,2})(/|$)')
+language_code_prefix_re = _lazy_re_compile(r'^/(\w+([@-]\w+){0,3})(/|$)')
```
**变异语义**：把上限从 `{0,2}` 放宽到 `{0,3}`，允许多达三个子标签。三段式仍匹配，但 `/de-simple-page-test/`（`de`+三个 dash 段）会被**误当作语言前缀**捕获，再交给语言匹配，使 `test_page_with_dash` 期望的"不匹配为语言码"失败。模拟"多放宽一点更保险"的上界 off-by-one。
