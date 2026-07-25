# Task 2 报告:MCP 引用检查分页(删除卫生 follow-up 打包)

## STATUS

DONE —— brief 六步全走完,TDD 先红后绿,变异自验跑了两个变异体、均被杀。

## Commits

- `fix(control-plane): MCP server 引用检查分页(>1 页租户的在用引用曾被漏检)`

## 结论

删 MCP server 的引用检查从「单页 `limit=1000`」改成「按 `_SPEC_PAGE_SIZE` 循环翻完整租户」。
租户 spec 超过一页时,第 1001 份起的在用引用不再被漏检,不会再误删在用 server。

## 前置核实:`list_by_tenant` 接受 `offset`

无需扩接口。

- 抽象基类 `packages/expert-work-persistence/src/expert_work/persistence/agent_spec/base.py:77`
  签名 `(*, tenant_id, status=None, name=None, limit=100, offset=0) -> list[AgentSpecRecord]`,
  docstring 明写 "Paginated list, newest first"。
- SQL 实现 `.../agent_spec/sql.py:127`:`.order_by(created_at.desc()).limit(limit).offset(offset)`。
- In-memory 实现 `.../agent_spec/memory.py:104`:`matched.sort(key=created_at, reverse=True)` 后
  `matched[offset : offset + limit]`。

两个实现排序谓词一致(created_at 倒序),分页语义等价,不存在 SQL↔in-memory 分歧。

## 变更文件

### `services/control-plane/src/control_plane/api/mcp_servers.py`

1. 新增模块级常量 `_SPEC_PAGE_SIZE = 200`(紧邻既有 `_DEFAULT_TIMEOUT_S`),与同域
   `agent_templates.py` 的 `_DEPENDENT_PAGE_SIZE = 200` 对齐。
2. `delete_mcp_server` 端点 (b) 段单页取数换成 brief 给的循环形状(`while True` +
   `len(page) < _SPEC_PAGE_SIZE` 终止),照抄 `_find_extends_dependents` 的姿态。
3. `from expert_work.protocol import AgentSpecRecord` —— 只为 `specs: list[AgentSpecRecord]` 这个标注。

**没动的东西(brief 明令)**:

- 下游三段 `active_specs`(过滤 DELETED)/ `referencing` / `implicit_all` 逐字未动。
- 「每 spec 单次 `model_dump`」的 `dumped = [...]` 形状未动。
- PR2 加的 (c2) 密文清理块(删除行之后)未碰。
- 循环整体在既有 `if agent_spec_store is not None:` 守卫内 —— 最小部署(spec store 未接线)
  仍然一次都不调。

### `services/control-plane/tests/test_mcp_servers_api.py`

新增 `test_delete_conflicts_when_reference_is_on_a_later_spec_page`,插在
`test_delete_ignores_soft_deleted_agent_reference` 之后:

- `monkeypatch.setattr(..., "_SPEC_PAGE_SIZE", 2)` 把页大小缩到 2,只播 3 份 spec 即跨页(控时长)。
- 唯一引用 github 的 `page-two-consumer` **最先**播种 —— store 是 newest-first,所以它排最后、落第二页。
  两份 filler 引用 `unrelated`(不用空 `servers`,免得撞上 implicit-all 语义)。
- **前提守卫**:先直接问 store 要第一页,断言 `page-two-consumer` 不在里面。
  没有这个守卫,万一 `created_at` 撞到同一微秒(稳定排序退化成插入序),referencing spec 会漂到
  第一页,哨兵就会静默地在「根本没翻页」的情况下变绿。
- **翻页取数记录**:包一层 `repo.list_by_tenant` 记录每次调用的 `(limit, offset)`,断言调用次数 > 1
  且每次 limit 都等于被 monkeypatch 的页大小。理由见「变异自验 · 变异体 B」。
- 断言里没有副作用(记录发生在 wrapper 内,断言只读已记录的 list);未新增任何日志,
  更没有请求派生值进日志。

## TDD 轨迹

| 步 | 状态 | 证据 |
|---|---|---|
| Step 1 写测试 | — | 新增跨页哨兵 |
| Step 2 确认红(形式红) | RED | `AttributeError: module 'control_plane.api.mcp_servers' has no attribute '_SPEC_PAGE_SIZE'` |
| Step 2' 确认红(实质红) | RED | 先只加常量、仍单页 `limit=_SPEC_PAGE_SIZE` → `AssertionError: assert 200 == 409`,响应体 `{"success":true,"data":{"implicit_all_agents":0},"error":null}` —— 在用 server 被真删掉了,正是要修的 bug |
| Step 3 实现 | — | 分页循环 |
| Step 4 确认绿 | GREEN | `test_mcp_servers_api.py` 39 passed |

「形式红」只证明常量不存在、不证明 bug,所以额外补了 Step 2' 的实质红 —— 让红的原因就是
「在用 server 被放过」。

## Step 5 变异自验

两个变异体,都在 `test_mcp_servers_api.py` 全量下验证。

### 变异体 A:循环换回单页、页大小走常量

```python
specs: list[AgentSpecRecord] = await agent_spec_store.list_by_tenant(
    tenant_id=tenant_id, limit=_SPEC_PAGE_SIZE
)
```

**杀掉。** `1 failed, 38 passed`,失败点 `assert 200 == 409` —— 第二页的引用没看见,server 被删。
恢复循环后 `39 passed`。

### 变异体 B:循环换回单页、页大小写死 1000(= 修复前的原样代码)

```python
specs: list[AgentSpecRecord] = await agent_spec_store.list_by_tenant(
    tenant_id=tenant_id, limit=1000
)
```

**第一版测试没杀掉(存活)。** 3 份 spec 全塞得进 1000 行的单页,引用照样被发现、409 照常返回、测试绿。
这是 brief Step 5 预期与现实之间的一个真实缺口:brief 写的是「换回单页 `limit=1000` → 跨页测试红」,
但「纯 monkeypatch 页大小 + 只播 3 份 spec」的策略对写死的大页面是瞎的。

处理:给哨兵补了「翻页取数记录」断言(不改任何既有断言,只加)。补完之后 ——

**杀掉。** `AssertionError: [(1000, 0)]` / `assert 1 > 1` —— 只发了一次请求、limit 还不是被
monkeypatch 的页大小,一眼看出退化成单页。

补完记录断言后重跑变异体 A:仍然被杀(`assert 200 == 409` 先炸)。恢复正确实现:`39 passed`。

替代方案是播 1001 份 spec 让真实默认页大小(200)自然跨页、完全不依赖 monkeypatch,
但 brief 明确「为控时长」不走这条,所以用记录断言补上盲区。

## 验证汇总

| 检查 | 结果 |
|---|---|
| `pytest services/control-plane/tests/test_mcp_servers_api.py` | **39 passed** |
| `pytest services/control-plane/tests -m "not integration"` | 2110 passed, 6 failed(全在 `test_eval_engine_live.py`,既有) |
| `ruff check services/control-plane` | All checks passed |
| `ruff format --check`(两个改动文件) | 2 files already formatted |
| mypy(两个改动文件) | 无新增错误 |

**那 6 个 failed 是既有的、与本改动无关**:全是 `ModuleNotFoundError: No module named 'tools'`;
把改动 `git stash` 掉后同样 6 个 failed。是「pytest 目标收窄到 `services/control-plane/tests` 时
根目录 `tools` 包不在 sys.path」的调用姿势产物(CI 从仓库根跑 `uv run pytest`,testpaths 含 `tools/*`,
不会碰到)。

**mypy**:CI 的 mypy 范围不含 control-plane(`.github/workflows/ci.yml:75`)。手工跑了改动的两个文件,
新代码零错误;报出的都是既有噪声 —— `mcp_servers.py` 那几条 `Unused "type: ignore"` 落在
170/174/178/182/345 行的 `_get_*` DI 函数上(未触碰,且是单文件 mypy 的经典假阳);
测试文件那条 `ASGITransport(app=app)` arg-type 是整个文件每个测试都有的既有形状
(`_make_app_with_admin` 返回 `object`)。

## Concerns

1. **变异体 B 暴露的 brief 缺口已就地补上,但补法偏白盒。**「记录 store 调用参数」比纯黑盒断言更贴实现:
   将来若有人正当地重构成「每页发现引用就早退」,`len(sweep_calls) > 1` 仍成立(引用在第二页),
   所以没有过度锁死;但若改成一次性全量拉取(比如新增 `list_all_by_tenant`),这条断言会拦下来 ——
   届时需连同哨兵一起改。已刻意用宽松形式(`> 1` + limit 一致)而不是精确匹配 `[(2,0),(2,2)]`。

2. **无上限的全量扫描。** 循环会把租户全部 spec 读进内存并逐份 `model_dump`,没有硬顶。
   同域的 `_find_extends_dependents` 也是这个形状(甚至是跨租户的 `list_all_tenants`),
   所以本改动只是与既有姿态对齐、没引入新量级问题;但「删一个 MCP server 要 dump 全租户 spec」
   本身在超大租户上是 O(specs) 的开销。要收的话应下推成 store 层谓词查询(例如按 `spec->tools`
   JSON 过滤),那是独立一项,不在本任务范围。

3. **`_SPEC_PAGE_SIZE` 与 `agent_templates._DEPENDENT_PAGE_SIZE` 是两份 200。** 语义相同(反查扫描页大小)
   但各自定义。没合并成共享常量 —— 跨模块抽常量属超范围改动,且两者未来可能各自调优。
   记在这里,免得后面有人只改一处。
