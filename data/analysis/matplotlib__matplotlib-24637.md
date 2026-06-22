# matplotlib__matplotlib-24637

## 问题背景

`AnnotationBbox` 的 gid 未传给 renderer——用 `set_gid` 设了 gid 后存 svg，gid 标签不出现在输出里。其它 artist 在 #15087 已修复，AnnotationBbox 被漏掉了。Golden patch 在 `AnnotationBbox.draw` 里用 `renderer.open_group(self.__class__.__name__, gid=self.get_gid())` 开组、`renderer.close_group(...)` 关组，使 svg 输出包含 `<g id="...">` 分组。

## Golden Patch 语义分析

```python
def draw(self, renderer):
    ...
    if not self.get_visible() or not self._check_xy(renderer):
        return
    renderer.open_group(self.__class__.__name__, gid=self.get_gid())
    self.update_positions(renderer)
    ... draw arrow/patch/offsetbox ...
    renderer.close_group(self.__class__.__name__)
    self.stale = False
```
核心语义：**`draw` 必须调 `renderer.open_group(..., gid=self.get_gid())` 把 artist 的真实 gid 传给 renderer（并配对 `close_group`），svg 后端据此输出 `<g id="...">`**。关键点：`gid=self.get_gid()`（真实 gid，非 None/常量）、open/close 配对包住绘制。

F2P 测试 `test_backend_svg.py::test_annotationbbox_gid`：set_gid("a test for issue 20044")，savefig svg，断言 `<g id="a test for issue 20044">` 在输出中。

## 调用链分析

`fig.savefig(svg)` → `AnnotationBbox.draw(renderer)` → `renderer.open_group(name, gid=self.get_gid())` → svg 后端写 `<g id=gid>`。若 gid 传 None/常量、漏传、或门控开关，svg 输出缺用户 gid 分组。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 🟢 高质量 | 保留 | `gid=self.get_gid()`→`gid=None` |
| B | 🟢 高质量 | 保留 | 三元恒等于 None 的自相矛盾表达式 |
| C | ➕ 补充 | 重做 | `gid='annotationbbox'` 硬编码常量 |
| D | 🟢 高质量 | 保留 | 删 gid kwarg，用默认 None |
| E | 🟢 高质量 | 保留 | gid 传递藏到 pass_gid 开关后 |

原始 C==D（都 `open_group(name)` 无 gid）。保留 A、B、D、E，重做 C 为硬编码常量 gid。

## 各组 Mutation 分析

### Group A — 保留（C1 值：gid=None）
```diff
-        renderer.open_group(self.__class__.__name__, gid=self.get_gid())
+        renderer.open_group(self.__class__.__name__, gid=None)
```
**变异语义**：`gid=self.get_gid()` 写死成 `gid=None`——AnnotationBbox 绘制时不把自身 gid 传给 renderer，svg 输出缺 `<g id=...>`。还原原 bug。F2P 失败。保留。

### Group B — 保留（B3 条件：自相矛盾三元）
```diff
-        renderer.open_group(self.__class__.__name__, gid=self.get_gid())
+        renderer.open_group(self.__class__.__name__, gid=self.get_gid() if not self.get_gid() else None)
```
**变异语义**：`gid=self.get_gid() if not self.get_gid() else None`——gid 为真时走 else 取 None，gid 为假时取 gid（假值）。无论如何都得不到真实 gid。逻辑自相矛盾。svg 无 gid。F2P 失败。保留。

### Group C — 重做（C1 值：硬编码常量）
**原**：与 D 相同（`open_group(name)` 无 gid）。
**最终 mutation**：
```diff
-        renderer.open_group(self.__class__.__name__, gid=self.get_gid())
+        renderer.open_group(self.__class__.__name__, gid='annotationbbox')
```
**变异语义**：`gid=self.get_gid()` 换成常量 `gid='annotationbbox'`——svg 写入固定字符串而非用户设的 gid。F2P 断言用户 gid（'a test for issue 20044'）不在输出中。模拟"传了硬编码 gid 而非真实的"。与 D（漏传致 None）不同——这里确实传了 gid、只是错的值。重做为 C。

### Group D — 保留（A2 接口契约：漏传 gid）
```diff
-        renderer.open_group(self.__class__.__name__, gid=self.get_gid())
+        renderer.open_group(self.__class__.__name__)
```
**变异语义**：删除 `gid=...` kwarg，只 `open_group(name)`——open_group 用默认 gid（None），svg 无用户 gid 分组。漏传关键参数。F2P 失败。保留。

### Group E — 保留（E2 隐式→显式开关）
```diff
-                 fontsize=None,
+                 fontsize=None,
+                 pass_gid=False,
                  **kwargs):
...
+        self.pass_gid = pass_gid
...
-        renderer.open_group(self.__class__.__name__, gid=self.get_gid())
+        renderer.open_group(self.__class__.__name__, gid=self.get_gid() if self.pass_gid else None)
```
**变异语义**：gid 传递藏到 `AnnotationBbox(pass_gid=False)` 参数后（默认 False）。默认 open_group 传 gid=None，svg 无 gid。只有显式开启才传真实 gid。模拟"把 gid 传递做成可配置、默认却关掉"。F2P 失败。保留。

## 新设计 Mutation 说明

原始 C==D 字节相同（`open_group(name)` 无 gid）。本次保留 A（gid=None）、B（自相矛盾三元）、D（删 kwarg）、E（pass_gid 默认关闭开关），重做 C 为硬编码常量 gid（确实传 gid 但值错，与 D 的漏传区分）。五组覆盖"gid=None / 矛盾三元 / 硬编码常量 / 漏传 kwarg / 默认关闭开关"五个角度——全部令 svg 输出缺用户的真实 gid 分组。全部实测（Python 3.9/matplotlib 3.6.0，源码构建 C 扩展，conda 编译器）：golden 通过、五个变异均令 F2P 失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
