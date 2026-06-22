# django__django-14011

> **路径 B（新实例）**：本实例不在 `mutations.jsonl` 中，无既有 mutation。按工作流路径 B，从 `processed_swe_bench.json` 获取元数据，从本地仓库 git 历史定位修复 commit，并为 A/B/C/D/E 五个策略组各设计一个全新的高质量 mutation。

## 问题背景

`LiveServerTestCase` 在 #20238 之后改用 `ThreadedWSGIServer`（`ThreadingMixIn` + `WSGIServer`）。每个 HTTP 请求在独立线程中处理，但请求结束后**没有关闭该线程持有的数据库连接**。这复活了老 bug #22414：测试结束 `destroy_test_db()` 时，因仍有未关闭的连接而抛 `OperationalError: database is being accessed by other users`（一个竞态，约半数运行复现）。

修复对应上游 ticket **#32416**，由两个 commit 组成（`71a936f9d8` + `823a9e6bac`，均为 base_commit `e4430f22` 的后代）：
1. `django/core/servers/basehttp.py`（**本实例可变源文件**）：给 `ThreadedWSGIServer` 增加连接管理——接收父线程传入的 `connections_override`、在每个请求线程开始时把这些连接注入本线程的 `connections` 注册表、并在每个请求结束（`close_request`）时调用 `connections.close_all()` 关闭它们。
2. `django/db/backends/sqlite3/features.py`（**本实例可变源文件**）：把新测试 `LiveServerTestCloseConnectionTest.test_closes_connections` 加入"in-memory sqlite 下 `close()` 为 no-op"的跳过列表。
3. `django/test/testcases.py`、`tests/servers/tests.py`（**测试文件**）：抽出 `_make_connections_override()` 钩子、调整 `LiveServerThread._create_server` 透传 `connections_override`、并新增 F2P 测试。

## Golden Patch 语义分析

`basehttp.py` 中 `ThreadedWSGIServer` 新增四段逻辑：

```python
def __init__(self, *args, connections_override=None, **kwargs):
    super().__init__(*args, **kwargs)
    self.connections_override = connections_override

def process_request_thread(self, request, client_address):
    if self.connections_override:
        for alias, conn in self.connections_override.items():
            connections[alias] = conn          # 把父线程的连接注入本请求线程
    super().process_request_thread(request, client_address)

def _close_connections(self):
    connections.close_all()                    # 供测试 mock 的间接层

def close_request(self, request):
    self._close_connections()                  # 每个请求结束后关闭连接
    super().close_request(request)
```

核心语义链条（缺一环则连接关不掉）：
1. **保存**父线程传入的 `connections_override`（一个 `{alias: conn}` 字典）。
2. **注入**：请求线程启动时，把这些共享连接写进**本线程局部**的 `connections` handler，使服务器线程操作的就是父线程那个 `conn` 对象——必须发生在 `super().process_request_thread`（真正处理请求、建立连接）**之前**。
3. **关闭**：请求结束 `close_request` 时 `connections.close_all()`，关闭的正是被注入的共享连接。

F2P 测试 `test_closes_connections`：父线程把 `CONN_MAX_AGE` 设为 `None`（阻止 Django 自身按存活期关连接）、`conn.connect()` 打开连接并断言 `conn.connection is not None`；发一个请求 `/model_view/`（服务器读到数据）；等 `_connections_closed` 事件后断言 `conn.connection is None`。由于服务器线程关闭的就是父线程那个共享 `conn`，父线程随后看到 `connection` 变 `None`。

## 调用链分析

- `LiveServerTestCase.setUpClass`（testcases.py，测试侧）调用 `_make_connections_override()` 得到 `{DEFAULT_DB_ALIAS: conn}`，`inc_thread_sharing()` 后经 `_create_server_thread → LiveServerThread._create_server → ThreadedWSGIServer(connections_override=...)` 传入 server。
- 每来一个请求：`socketserver.ThreadingMixIn.process_request` 起新线程跑 `process_request_thread` → 注入连接 → `super().process_request_thread`（WSGI 处理，访问 DB）→ 处理完 `close_request` → `_close_connections()` → `connections.close_all()`。
- **关键数据流区分**（决定 mutation 能否只命中 F2P）：
  - **普通 live-server 测试**（`LiveServerViews`、`LiveServerDatabase` 等 24 个）：用文件型/内存 sqlite，`_make_connections_override` 仅收集 *in-memory* sqlite 连接；在 SWE-bench 的**文件型** sqlite 配置下，`connections_override` 是**空字典 `{}` 甚至 `None`**——它们根本不依赖注入/关闭逻辑，请求线程用自己的连接。
  - **F2P 测试**：覆写 `_make_connections_override` 返回 `{DEFAULT_DB_ALIAS: conn}`（**恰好 1 个**真实共享连接），是**唯一**真正走"注入 + 关闭共享连接"全链路的测试。
- 这一不对称是所有 mutation 的设计支点：凡是只破坏"非空且单元素 `connections_override`"路径的变异，都只让 F2P 失败、不波及其余 25 个测试。
- `features.py` 的跳过列表仅在 *in-memory* sqlite 下生效；SWE-bench 用文件型 sqlite 运行该 F2P（否则会被 skip），故对 `features.py` 的改动不影响判定，本组所有 mutation 都落在 `basehttp.py`。

## 替换决策总览

> 路径 B：无既有 mutation，全部为新设计。下表"类别"列标注新设计变异所属的策略维度。

| 组 | 类别 | 决策 | 原因摘要 |
|---|---|---|---|
| A | 新设计 A1（参数语义） | 新增 | `__init__` 改从 `kwargs.get('connections_override')` 取值，但该参数是命名参数已被 `*args/**kwargs` 之外捕获，`kwargs` 里恒无此键 → 恒 `None`，连接永不注入 |
| B | 新设计 B1（off-by-one 边界） | 新增 | 注入条件加 `len(...) > 1` 守卫；F2P 的 override 恰好 1 个元素，被排除；普通测试 override 为空本就不进入，故只命中 F2P |
| C | 新设计 C1（类型/形态约定） | 新增 | 遍历 `connections_override` 时漏掉 `.items()`，对 dict 迭代得到的是 key（alias 字符串）而非 `(alias, conn)`，解包/注入错乱 |
| D | 新设计 D3（顺序依赖） | 新增 | 把 `super().process_request_thread`（处理请求、建连）挪到连接注入**之前**，请求已用旧连接处理完，注入来得太晚，关闭的不是父线程的共享连接 |
| E | 新设计 E2（隐式行为→显式开关） | 新增 | 新增默认 `False` 的类属性 `close_connections_on_request` 门控 `close_request` 中的关闭调用，默认即不关闭 |

语义浅层共 0 个；新设计 5 个（A/B/C/D/E），全部为高质量变异。

## 各组 Mutation 分析

五个变异都作用于 `basehttp.py` 的 `ThreadedWSGIServer`，但分布在五个互不重叠的语义维度，且都利用了"只有 F2P 传入非空单元素 `connections_override`"这一不对称，使其余 25 个 live-server 测试不受影响。

### Group A — 新设计（A1 参数默认/取值语义）
**最终 mutation**：
```diff
     def __init__(self, *args, connections_override=None, **kwargs):
         super().__init__(*args, **kwargs)
-        self.connections_override = connections_override
+        self.connections_override = kwargs.get('connections_override')
```
**变异语义**：`connections_override` 是一个**命名关键字参数**（`def __init__(self, *args, connections_override=None, **kwargs)`），调用方 `connections_override=...` 会绑定到这个形参，**不会**落进 `**kwargs`。因此 `kwargs.get('connections_override')` 永远取不到值、恒返回 `None`。结果：`self.connections_override` 恒为 `None`，`process_request_thread` 的 `if self.connections_override:` 恒假，父线程的共享连接从不注入服务器线程；服务器线程用自己的连接处理请求并关闭，父线程的 `conn.connection` 仍非 `None`，F2P 失败。其余测试本就传空/`None`，行为不变。这是真实开发者极易犯的"该读形参却去 `kwargs.get` 同名键"的混淆——两处都叫 `connections_override`，单看 `__init__` 体感觉只是换了等价的取值方式，实则因 Python 参数绑定规则而恒空。属 A1（参数取值语义错误）。

### Group B — 新设计（B1 off-by-one 边界）
**最终 mutation**：
```diff
     def process_request_thread(self, request, client_address):
-        if self.connections_override:
+        if self.connections_override and len(self.connections_override) > 1:
             # Override this thread's database connections with the ones
             # provided by the parent thread.
             for alias, conn in self.connections_override.items():
                 connections[alias] = conn
```
**变异语义**：给注入条件加上 `len(self.connections_override) > 1` 的边界守卫，伪装成"只有需要覆写多个数据库连接时才值得做注入"的优化。但 F2P 的 `connections_override` 恰好是 `{DEFAULT_DB_ALIAS: conn}`——**只有 1 个元素**，`1 > 1` 为假，注入被跳过，共享连接不被关闭，F2P 失败。而其余 25 个测试在文件型 sqlite 下 `connections_override` 为空（不满足前半 `and`），本就不进入注入分支，行为完全不变——因此**只**命中 F2P。审查者看到 `len > 1` 容易理解成"多连接才需特殊处理"，却忽略了**单连接才是该功能的核心场景**。属 B1（off-by-one：应为"非空即处理"却写成"多于一个才处理"，把单元素边界排除）。

### Group C — 新设计（C1 类型/数据形态约定）
**最终 mutation**：
```diff
         if self.connections_override:
             # Override this thread's database connections with the ones
             # provided by the parent thread.
-            for alias, conn in self.connections_override.items():
+            for alias, conn in self.connections_override:
                 connections[alias] = conn
```
**变异语义**：遍历字典时漏写 `.items()`。直接 `for ... in dict` 迭代的是**键**（`alias` 字符串，如 `'default'`），而代码却用 `alias, conn` 解包——Python 会尝试把字符串 `'default'` 解包成两个字符，要么 `ValueError`（长度不为 2），要么把单个字符当 alias/conn，注入逻辑彻底错乱、连接无法正确注入。请求线程随之异常，服务器以 `ConnectionResetError` 中断，F2P 失败（ERROR）。其余测试 `connections_override` 为空，`for` 循环零次迭代，不触发该 bug，正常通过。这是非常自然的"忘了 `.items()`"——遍历 dict 时漏掉 `.items()` 是高频笔误，且 `for alias, conn in something:` 读起来语法完全合理，只有意识到 `connections_override` 是 dict 才能发现。属 C1（破坏隐式数据形态：dict 迭代产出 key 而非 `(key, value)` 对）。

### Group D — 新设计（D3 顺序依赖）
**最终 mutation**：
```diff
     def process_request_thread(self, request, client_address):
+        super().process_request_thread(request, client_address)
         if self.connections_override:
             # Override this thread's database connections with the ones
             # provided by the parent thread.
             for alias, conn in self.connections_override.items():
                 connections[alias] = conn
-        super().process_request_thread(request, client_address)
```
**变异语义**：把 `super().process_request_thread(...)`（真正处理 HTTP 请求、在本线程内建立并使用数据库连接）从注入逻辑**之后**挪到**之前**。语义要求是"先把父线程的共享连接注入本线程，再处理请求"，这样请求用的就是共享连接、结束时 `close_request` 关闭的也是它。反转顺序后：请求先用本线程**自己新建**的连接处理完，注入在请求处理结束后才发生（且为时已晚）；`close_request` 后续关闭的连接与父线程持有的共享 `conn` 不是同一个，父线程的 `conn.connection` 仍非 `None`，F2P 失败。其余测试 override 为空，两条语句顺序无关，正常通过。这是典型的"两步操作顺序写反"——两行都在、逻辑看似完整，唯有理解"注入必须先于请求处理"这一时序契约才能发现。属 D3（顺序依赖：初始化/注入与使用的先后被破坏）。

### Group E — 新设计（E2 隐式行为→显式参数门控）
**最终 mutation**：
```diff
+    close_connections_on_request = False
+
     def _close_connections(self):
         # Used for mocking in tests.
         connections.close_all()

     def close_request(self, request):
-        self._close_connections()
+        if self.close_connections_on_request:
+            self._close_connections()
         super().close_request(request)
```
**变异语义**：新增一个默认 `False` 的类属性开关 `close_connections_on_request`，把"每个请求结束后关闭连接"从无条件行为变成需显式启用的可选行为。看起来像一次合理的"提供可配置开关、避免在不需要时强行关连接"的设计增强，类属性 + 条件门控都符合常见代码风格。但默认 `False` 意味着 `close_request` 默认**不**调用 `_close_connections()`，连接永不关闭——正是 golden patch 要修复的原始 bug。F2P 等不到连接关闭（`_connections_closed` 事件不触发、`conn.connection` 仍非 `None`），失败。其余测试不依赖该关闭行为，正常通过。审查者只会觉得多了个无害的特性旗标，而不会意识到**默认值把修复关掉了**。属 E2（隐式行为被改为显式开关，默认值使其失效）。

## 新设计 Mutation 说明

本实例为路径 B，五个变异均为全新设计，建立在对 #32416 修复语义与测试数据流的深层理解上，分别攻击五个互不重叠的维度，且都规避了人工痕迹（无注释式 "Bug"、无硬编码魔数、无逻辑矛盾）：

- **A（A1）**：利用 Python "命名关键字参数不进 `**kwargs`" 的规则，用 `kwargs.get(同名键)` 制造恒 `None`，伪装成等价取值。
- **B（B1）**：利用 F2P 的 `connections_override` 恰好单元素、普通测试为空的不对称，用 `len > 1` 把单元素边界排除，伪装成"多连接才优化"。
- **C（C1）**：漏写 `.items()`，利用 dict 迭代产出 key 的形态约定，伪装成等价遍历。
- **D（D3）**：反转 `super()` 与注入的先后，利用"注入须先于请求处理"的时序契约，伪装成无关紧要的语句顺序。
- **E（E2）**：新增默认关闭的开关门控连接关闭，伪装成可配置增强。

全部仅修改 `django/core/servers/basehttp.py`（允许的源文件之一），不触碰测试文件。均通过 Step 5 实证自查：在 base_commit → golden patch → test_patch 之后用 `git diff HEAD` 生成、`py_compile` 通过，并在**文件型 sqlite**（`test_sqlite_file` 设置，使 F2P 实际运行而非被 in-memory 跳过）下实际运行整个 `servers` 测试套件（26 个测试）确认：每个变异都**只**使 F2P 测试 `servers.tests.LiveServerTestCloseConnectionTest.test_closes_connections` 失败（A/B/D/E 为断言 `conn.connection is not None` 的 FAILURE，C 为请求线程崩溃的 ConnectionResetError ERROR），其余 25 个测试全部通过（无附带破坏）。
