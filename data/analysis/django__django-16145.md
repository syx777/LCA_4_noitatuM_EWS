# django__django-16145

## 问题背景

`python manage.py runserver 0:8000` 应等价于 `0.0.0.0:8000`，但启动提示却打印 `Starting development server at http://0:8000/`，链接 `http://0:8000/` 在浏览器中不可用。Golden patch 在 `inner_run` 计算显示用 `addr` 时新增一段：`_raw_ipv6` 则 `[addr]`，否则若 `addr == "0"` 则显示为 `"0.0.0.0"`，其余照原样。

## Golden Patch 语义分析

```python
if self._raw_ipv6:
    addr = f"[{self.addr}]"
elif self.addr == "0":
    addr = "0.0.0.0"
else:
    addr = self.addr
...
"addr": addr,   # 原为 "[%s]" % self.addr if self._raw_ipv6 else self.addr
```
核心语义：**显示地址时，`"0"` 这个简写必须展开成 `"0.0.0.0"`**，与文档一致、链接可点。新增的 `elif self.addr == "0"` 分支专门处理这一简写；`_raw_ipv6` 分支保留方括号格式；`else` 保留原值。把原先内联在格式化字典里的三元表达式提取为前置的 `addr` 变量，并补上 `"0"` 的特例。`self._raw_ipv6` 在 `handle` 中初始化（默认 False，解析 ipv6 地址时设 True），是该分支判断的前置状态。

F2P 测试 `ManageRunserver.test_zero_ip_addr`：`runserver 0:8000`，断言输出含 `Starting development server at http://0.0.0.0:8000/`。

## 调用链分析

`handle` 解析 addrport，设 `self.addr`/`self.port`/`self._raw_ipv6`，调 `self.run` → `inner_run`。`inner_run` 据 `_raw_ipv6`/`addr=="0"` 算显示 `addr`，写入启动提示。`_raw_ipv6` 必须先在 `handle` 初始化为 False，否则 `inner_run` 里 `if self._raw_ipv6` 访问未定义属性会 AttributeError。`addr=="0"` 分支是修复核心；条件、映射值、变量初始化任一出错都会让 `0` 显示错误或崩溃。

## 替换决策总览

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | ➕ 补充 | 新增 | 原缺 A；`"0.0.0.0"`→`"0.0.0"`，展开成错误地址字面量 |
| B | 🟢 高质量 | 保留 | `== "0"`→`!= "0"`，条件反转 |
| C | 🟢 高质量 | 保留 | 删除 `elif addr=="0"` 分支，"0" 不再展开 |
| D | 🟢 高质量 | 保留 | 删除 `_raw_ipv6 = False` 初始化，inner_run 访问未定义属性 |
| E | 🔴 必须替换 | 替换 | 原 E 与 C 字节完全相同（删分支）；改为默认关闭开关 |

原 C、E 字节完全相同（删 `elif addr=="0"` 分支），缺 A。补充 A、重做 E，保留 B、C、D。

## 各组 Mutation 分析

### Group A — 补充（C1 值/数据形状：错误地址字面量）
```diff
         elif self.addr == "0":
-            addr = "0.0.0.0"
+            addr = "0.0.0"
```
**变异语义**：`"0"` 被展开成 `"0.0.0"`（只有三段、缺一段）而非 `"0.0.0.0"`。条件分支结构正确、确实展开了，但目标字面量写错。输出 `http://0.0.0:8000/`，F2P 断言 `0.0.0.0` 失败。模拟"手敲 IP 字面量时少打一段"的低级但真实的错误，比删分支隐蔽——分支逻辑看起来完全对。

### Group B — 保留（B3 条件反转）
```diff
-        elif self.addr == "0":
+        elif self.addr != "0":
```
**变异语义**：条件反转。`addr != "0"` 时（几乎所有正常地址如 `127.0.0.1`）被错误地改写成 `"0.0.0.0"`，而真正的 `"0"` 落入 else 保持 `"0"`。正常地址显示全错、`"0"` 反而没展开。保留。

### Group C — 保留（B2 删除分支）
```diff
         if self._raw_ipv6:
             addr = f"[{self.addr}]"
-        elif self.addr == "0":
-            addr = "0.0.0.0"
         else:
             addr = self.addr
```
**变异语义**：删除 `"0"` 特例分支，`"0"` 落入 else 保持 `"0"`，输出 `http://0:8000/`，还原原 bug。保留。

### Group D — 保留（D4 状态：删除属性初始化）
```diff
             raise CommandError("Your Python does not support IPv6.")
-        self._raw_ipv6 = False
```
**变异语义**：删除 `handle` 中 `self._raw_ipv6 = False` 的初始化。`inner_run` 里 `if self._raw_ipv6:` 访问从未设置的属性，抛 `AttributeError: 'Command' object has no attribute '_raw_ipv6'`。这是跨方法的状态初始化遗漏——bug 根因（删 init）与表现（inner_run 崩溃）分离在两个方法，难以一眼定位。模拟"删/漏了对象状态初始化"。保留。

### Group E — 替换（E2 隐式→显式开关）
**原**：与 C 字节完全相同（删 `elif addr=="0"` 分支）。
**最终 mutation**：
```diff
         elif self.addr == "0":
+        elif self.addr == "0" and getattr(self, "expand_zero_addr", False):
             addr = "0.0.0.0"
```
**变异语义**：在 `addr == "0"` 条件后追加开关 `expand_zero_addr`，默认 `False`。`"0"` 即便匹配，因 `and False` 不进入该分支 → 落入 else 保持 `"0"`（旧 bug）。只有显式设 `expand_zero_addr=True` 才展开。模拟"把展开做成可配置、默认却关掉"。与 C（删分支）机制不同。

## 新设计 Mutation 说明

原 C、E 字节完全相同（都删 `elif addr=="0"` 分支），缺 A。本次保留 B（条件反转）、C（删分支）、D（删 `_raw_ipv6` 初始化，跨方法状态遗漏），补充 A（`"0.0.0.0"`→`"0.0.0"` 错误字面量），把与 C 重复的 E 重做为默认关闭的 `expand_zero_addr` 开关。五组覆盖"错误字面量 / 条件反转 / 删分支 / 删状态初始化 / 默认关闭开关"五个角度，分布在 `handle`（D）与 `inner_run`（A/B/C/E）两个方法。全部实测：golden 通过、五个变异均令 F2P（`test_zero_ip_addr`）失败、`base→golden→test_patch` 后干净应用、`py_compile` 通过。
