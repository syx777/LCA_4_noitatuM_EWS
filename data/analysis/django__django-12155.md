# django__django-12155 Mutation 策展分析

## 问题背景

`django/contrib/admindocs` 在渲染视图/模型 docstring 时，若 docstring 首行非空（文本从第一行开始，而非 PEP 257 风格的首行为空），`docutils` 会报错：

```
Error in "default-role" directive: no content permitted.
```

根因在旧的 `trim_docstring`：

```python
indent = min(len(line) - len(line.lstrip()) for line in lines if line.lstrip())
```

首行缩进恒为 0，导致 `indent==0`，后续行的公共缩进无法被剥离，body 中残留缩进使 reST 解析异常。

## Golden Patch 语义分析

Golden 的修复：
- 删除自定义的 `trim_docstring`，改用标准库 `inspect.cleandoc`。`cleandoc` 的关键语义是**计算公共缩进时跳过首行**（首行单独 lstrip，其余行按统一缩进剥离），从而正确处理首行非空的情形。
- `parse_docstring` 中 `docstring = trim_docstring(docstring)` 改为先 `if not docstring: return '', '', {}` 再 `docstring = cleandoc(docstring)`。
- `views.py` 中 `utils.trim_docstring(verbose)` 同步改为 `cleandoc(verbose)`。

## 调用链分析

`parse_docstring(docstring)` → `cleandoc` 归一缩进 → `re.split(r'\n{2,}')` 切分段落 → `title=parts[0]`，其余进入 `HeaderParser` 解析 metadata：
- 若解析出非空 metadata：`body = parts[1:-1]`（末段当 metadata）；
- 否则（HeaderParseError 或空 metadata）：`body = parts[1:]`（全部留作 body）。

F2P `test_parse_rst_with_docstring_no_leading_line_feed` 输入 `'firstline\n\n    second line'`：
- `cleandoc` 后 second line 的 4 空格缩进被剥离 → `parts=['firstline','second line']`；
- HeaderParser 对 `'second line'` 解析得空 metadata，走 **else 分支** `body=parts[1:]='second line'`；
- 断言 `parse_rst(body,'')=='<p>second line</p>\n'` 且 stderr 为空。

P2P（`test_parse_docstring` / `test_description_output` / `test_title_output`）使用类 `__doc__`——首行为空、统一缩进、含 `some_metadata: some data`，因此走**非空 metadata 分支**。

关键发现：F2P 唯一走 “空 metadata else 分支”，而 P2P 全部走 “非空 metadata 分支”，且 P2P docstring 首行为空，因此对 “首行特殊处理” 或 “else 分支切片” 的细微改动只会破坏 F2P 而不影响 P2P。这是设计正交、精准变异的着力点。

## 替换决策总览

| 原组 | 原 diff 摘要 | 分类 | 决策 | 最终 strategy_code |
|------|-------------|------|------|--------------------|
| B | `if metadata` → `if not metadata`（取反非空分支条件） | 🔴 破坏 P2P | 替换 | B2 |
| D | `cleandoc` 改为仅 lstrip 首行、保留其余缩进 | 🔴 破坏 P2P | 替换 | D2 |
| E | 给 `parse_docstring` 增 `use_cleandoc=False` 参数，默认跳过 cleandoc | 🔴 破坏 P2P | 替换 | E1 |

三个原始 mutation 经真实运行均**同时破坏多个 P2P 测试**（`test_parse_docstring`、`test_description_output`、`test_title_output`），即它们对共享的缩进/分支处理改动过于宽泛，普通 docstring 测试即可捕获 → 全部判 🔴 MUST REPLACE。共 3 个全部替换。

## 各组 Mutation 分析

### Group B
- 原 diff：`if metadata:` → `if not metadata:`，反转 body 切片分支条件。
- 分类：🔴。实测 P2P `test_parse_docstring` 等失败（非空 metadata 场景下 body 取错），过于显眼。
- 最终 diff：在**空 metadata 的 else 分支**把 `body = "\n\n".join(parts[1:])` 改为 `parts[1:-1]`（off-by-one）。
- 变异语义：仅当 metadata 为空时多丢弃最后一段 body。P2P 走非空分支不受影响；F2P 走空分支，`body` 由 `'second line'` 变为 `''`，断言失败。failure mode：边界切片 off-by-one。

### Group D
- 原 diff：用手写 “仅 lstrip 首行、其余行原样保留” 替换 `cleandoc`。
- 分类：🔴。实测完全不剥离缩进，P2P 全崩。
- 最终 diff：`docstring = cleandoc(docstring)` → `docstring = dedent(docstring).strip()`（新增 `from textwrap import dedent`）。
- 变异语义：`textwrap.dedent` 计算公共缩进时**包含首行**，而 `cleandoc` 跳过首行。P2P docstring 首行为空、其余行统一缩进，dedent 仍能正确剥离（首行不影响公共前缀），故 P2P 通过；F2P 首行 `firstline` 缩进为 0 使公共前缀为空，`    second line` 缩进未被剥离，复现原始 bug，F2P 失败。failure mode：等价替换 helper 的语义差异。

### Group E
- 原 diff：新增 `use_cleandoc=False` 参数，默认关闭 cleandoc。
- 分类：🔴。默认完全不归一缩进，P2P 全崩；且改了函数签名（接口契约）易被察觉。
- 最终 diff：`docstring = cleandoc(docstring)` 改为 `if docstring.startswith('\n'): cleandoc(...) else: docstring.strip()`。
- 变异语义：编码 “所有 docstring 都以换行开头”（PEP 257 风格）的错误假设。P2P docstring 以 `\n` 开头，走 cleandoc 正常；F2P 输入 `'firstline...'` 不以 `\n` 开头，只做 `strip()` 不剥离内部缩进，F2P 失败。failure mode：条件组合 / 隐式输入假设。

## 新设计 Mutation 说明

三者构成正交的失败模式集合：
- B2 — 分支内边界切片错误（数据流末段丢失）；
- D2 — 跨函数语义差异（dedent vs cleandoc 的首行处理）；
- E1 — 条件分支上的隐式输入假设（首行换行假设）。

均为真实可信的开发者错误，普通 docstring 用例无法察觉，只有针对 “首行非空” 场景的 F2P 才暴露。

## 真实验证结果

环境：`/usr/local/bin/python3`（3.8.12），需 `python3 -m pip install docutils`（系统 `pip` 指向 py2.7，需显式用 py3）。

- 基线（golden，无变异）：`admin_docs.test_utils` 全 7 测试 **PASS**，F2P PASS。
- Group B2：apply rc=0，py_compile OK，6 P2P **ok**，F2P **FAIL**。
- Group D2：apply rc=0，py_compile OK，6 P2P **ok**，F2P **FAIL**。
- Group E1：apply rc=0，py_compile OK，6 P2P **ok**，F2P **FAIL**。

所有最终 diff 均基于 golden+test_patch 之后的 POST-PATCH 内容生成，patch -p1 干净应用。
