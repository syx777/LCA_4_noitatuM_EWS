# django__django-13279 Mutation 策展分析

## 问题背景

在 Django 3.0 → 3.1 的过渡期，commit d4fff711 改变了 session 数据的编码格式（改用 `signing.dumps`）。
当多实例部署时，新旧实例需要互相读取对方写入的 session。仅设置 `DEFAULT_HASHING_ALGORITHM='sha1'`
不足以让新实例写出旧实例能解码的数据，因为 `encode()` 总是输出新格式。

修复方案：当 `settings.DEFAULT_HASHING_ALGORITHM == 'sha1'` 时，`encode()` 改为调用新提取的
`_legacy_encode()`，输出 pre-3.1 的 `base64(hash:serialized)` 旧格式，从而保证旧实例（以及
`_legacy_decode`）能正确解码。

涉及文件：`django/contrib/sessions/backends/base.py`
测试文件：`tests/sessions_tests/tests.py`

## Golden Patch 语义分析

Golden patch 做两件事：

1. 在 `encode()` 开头插入控制流分支：
   ```python
   if settings.DEFAULT_HASHING_ALGORITHM == 'sha1':
       return self._legacy_encode(session_dict)
   ```
2. 新增 `_legacy_encode()` 方法，复现旧编码逻辑：
   ```python
   serialized = self.serializer().dumps(session_dict)
   hash = self._hash(serialized)          # hexdigest()，返回 str
   return base64.b64encode(hash.encode() + b':' + serialized).decode('ascii')
   ```
   注意 `_hash()` 返回 str（hexdigest），需 `.encode()` 转 bytes 才能与 `b':'`、`serialized`(bytes) 拼接。

## 调用链分析

- F2P 测试 `test_default_hashing_algorith_legacy_decode` 定义在 `SessionTestsMixin`（tests.py），
  因此在所有后端子类（CacheDB / Cache / Database / File / Cookie / Custom 等共 9 个类）中各跑一次。
- 测试逻辑：`with self.settings(DEFAULT_HASHING_ALGORITHM='sha1')` → `encoded = self.session.encode(data)`
  → `assertEqual(self.session._legacy_decode(encoded), data)`。
- 这是一个 encode → legacy_decode 的 round-trip 闭环：
  - 若 `encode()` 不走 `_legacy_encode`（走新格式），`_legacy_decode` 解析失败返回 `{}` → 断言失败。
  - 若 `_legacy_encode` 内部拼接出错（TypeError），encode 直接抛异常 → 测试 error。
- `self.session` 在 setUp 中 `self.backend()` 全新创建，`modified=False`（未修改）。

## 替换决策总览

| 组 | 原 diff | 分类 | 决策 | 最终策略 |
|----|---------|------|------|----------|
| A | `== 'sha1'` → `== 'sha256'` | 🟡 SHALLOW（最强：现实算法名值替换，非逻辑反转） | KEEP | A1 |
| B | `== 'sha1'` → `!= 'sha1'` | 🔴/🟡 最弱（golden 条件的直接逻辑反转，等价于 trivial revert 的镜像） | REPLACE | B3 |
| C | `hash.encode()` → `hash` | 🟢 类型强制丢失，跨语义 TypeError | KEEP | C1 |

M（shallow 单 token）= 2（A、B 均为条件单 token 改动）。floor(2/2)=1，替换最弱者 B。
C 为 str/bytes 类型强制语义错误，非单 token 简单交换，保留。

## 各组 Mutation 分析

### A 组（KEEP，A1）
- 原 diff：`if settings.DEFAULT_HASHING_ALGORITHM == 'sha1':` → `== 'sha256':`
- 分类：🟡 SHALLOW，但为两个 shallow 中较强者。
- 理由：把触发值改成另一个真实哈希算法名 `'sha256'`，不是 golden 条件的逻辑反转，看起来像“配置常量笔误”。
  在 sha1 过渡场景下，条件不再命中，`encode()` 走新格式，`_legacy_decode` round-trip 失败。
- 变异语义：A1（Alter Parameter/Semantics）—改变控制分支触发的参数值。
- 验证：单测 F2P FAILED；全模块仅 9 个 F2P 同名测试 error，无 P2P 破坏。

### B 组（REPLACE）
- 原 diff：`== 'sha1'` → `!= 'sha1'`
- 分类：🔴 直接冗余。这是 golden 新增条件的直接逻辑反转，等同于把 golden 控制流“镜像翻转”，
  属于最易被测试探测、与 golden 高度对称的人工痕迹（reverse-of-golden 风味），是两个 shallow 中最弱者。
- 决策：REPLACE。

### C 组（KEEP，C1）
- 原 diff：`base64.b64encode(hash.encode() + b':' + serialized)` → `base64.b64encode(hash + b':' + serialized)`
- 分类：🟢。删除 str→bytes 的隐式/显式强制转换，使 str(hexdigest) 与 bytes 拼接抛 TypeError。
- 理由：跨 str/bytes 边界的真实易犯错误；代码读起来像“误以为 hash 已是 bytes”。仅当真正走 sha1 legacy 编码路径时才触发，
  非单 token 同类型交换。保留。
- 变异语义：C1（Break Implicit Type Coercion）。
- 验证：单测 F2P FAILED（TypeError）；全模块仅 9 个 F2P error，无 P2P 破坏。

## 新设计 Mutation 说明

### B 组替换设计（B3）
- 最终 diff：
  ```python
  -        if settings.DEFAULT_HASHING_ALGORITHM == 'sha1':
  +        if settings.DEFAULT_HASHING_ALGORITHM == 'sha1' and self.modified:
               return self._legacy_encode(session_dict)
  ```
- 设计思路：在 legacy-encode 分支上叠加一个**看似合理的额外守卫** `and self.modified`，
  伪装成“只有 session 被修改过才需要写旧格式”的优化。这是条件组合（condition-combination）类、
  跨状态（self.modified 实例状态）的错误，与 A 的“值替换”、C 的“类型错误”形成正交失效模式。
- 为何难检测：测试中 `self.session` 通常是新建且未修改的（`modified=False`），多数仅做 encode 而不先改动
  session 的测试都看不出差别（条件因 `modified` 为假而短路到新格式）。任何在 encode 前修改 session 的测试反而会通过。
  只有“对全新数据直接 encode 并 legacy_decode”的 F2P 断言会暴露它。
- 验证：单测 F2P FAILED；全模块 `--parallel=1` 仅 9 个 F2P 同名测试 error，384 测试其余全过，无 P2P 破坏。

## 验证结果汇总

- 测试 harness：tmp 复制 base → 应用 golden patch (rc0) → 应用 test_patch (rc0) → git commit。
- BASELINE：golden 无变异时 F2P `CacheDBSessionTests.test_default_hashing_algorith_legacy_decode` PASS；
  全模块 384 tests OK (skipped=2, expected failures=1)。
- 三个最终变异（A1 / B3 / C1）均：patch 应用成功、py_compile OK、F2P FAILED。
- 全模块（--parallel=1）每个变异仅导致 9 个 `test_default_hashing_algorith_legacy_decode`（跨后端子类，全部源自 SessionTestsMixin）失败，
  无任何 P2P 回归。

> 注：runtests.py 并行模式因 Python 3.8 下 traceback 无法 pickle（缺 tblib）会崩溃，故全模块验证统一用 `--parallel=1`。
