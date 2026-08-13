# Backlog Task 6 — agent_private 技能补 owner 闸(报告)

分支 `spec/external-api-v1-p2`(worktree 分叉自会话 base,已 `git merge spec/external-api-v1-p2 --no-edit` fast-forward 到 `e6b61359`)。

## 状态

完成。10 个单条端点 403 + 列表端点过滤 + 3 条变异全部按预期观察到红/绿。
`test_skills_api.py`(43 passed)、`test_console_lockdown.py`(96 passed,零回归)均全绿。
`ruff check .` / `ruff format --check .` 均通过。

## Commit

`460c0d12`(`fix(control-plane): agent_private 技能补 owner 闸——堵租户内跨终端用户数据泄露`)

## 共享判定函数

`services/control-plane/src/control_plane/api/skills.py`,`_require_skill_owner_scope(skill: Skill, principal: Principal) -> None`,放在 `_version_dict` 之后、`build_skills_router()` 之前(模块级 helper 区,与 `_validate_supporting_file_path` 同一档次)。

```python
def _require_skill_owner_scope(skill: Skill, principal: Principal) -> None:
    if skill.visibility == "agent_private" and not is_admin(principal):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "SKILL_SCOPE_FORBIDDEN",
                "message": "this skill is agent-private; only a tenant admin may access it",
            },
        )
```

10 个端点全部改用它,零复制。`is_admin` 从 `control_plane.auth.rbac` 引入(与 brief 给的行号一致,`rbac.py:217`)。

## 403 还是 404

用 403 + `{"code": "SKILL_SCOPE_FORBIDDEN", "message": ...}`,照 `resolve_target_user_id`(`api/_user_scope.py:73-80`)先例的信封形状。

复核了 brief 提到的"skills.py 里是否有相反约定":该文件顶部文档字符串写"404 for cross-tenant / unknown",但那条先例管的是**跨租户**轴(`get_skill`/`get_version_by_number` 对不存在或别的租户的行返回 404,隐藏存在性),不是**租户内员工间**轴 —— 这两条轴在 skills.py 里从未被同一个判断处理过,不构成"相反约定"。本任务是全新的第三条轴,console_only() 已经把第三方 API key 挡在外面,能走到这里的只有员工凭证,403 不会向未认证方泄露信息,而对管理员排障更友好,所以采用 403。

## 列表过滤放在哪一层 + 分页影响

放在**store 查询层**(WHERE 子句),不是 handler 后置过滤。`SkillStore.list_skills` 已经接受一个可选的 `visibility` 关键字参数(SE-8 时期就有),且 `sql.py::_list_skills` 里 `visibility` 过滤是在 `LIMIT` 之前的 WHERE 子句(`sql.py:376-377,392`)。所以非 admin 调用者只需强制 `effective_visibility="tenant"`(该字段是闭合的二值 Literal `"agent_private"|"tenant"`,"强制 tenant"等价于"排除 agent_private"),复用既有查询参数即可,**没有改 store 层代码**,也就没有"取出分页结果之后过滤"导致"每页数量不足"的问题。

非 admin 显式传 `visibility=agent_private` 是唯一的特殊分支:此时不下发查询(直接 `rows, next_cursor = [], None`),因为把 `agent_private` 传给 store 会原样返回真实的私有行 —— 那正是要堵的洞。这个分支返回空页而非 403(brief §3 明确要求),且不影响同一响应里的 `platform_items`(那段逻辑在外层 `else` 块里,不受这个 if/else 影响,与过滤前的行为一致 —— `platform_items` 从未被 `visibility` 参数过滤过)。

CrossTenant(`tenant_id=*`)分支未改动:那条路径只有 `system_admin` 能走到(`ensure_tenant_scope` 的决策矩阵),而 `is_admin()` 对 `system_admin` 也返回 True,所以该分支的调用者必然已经是"admin"语义,不需要额外过滤。

## 10 个端点里,哪些需要额外一次 `get_skill` 调用

`visibility` 字段只在 `Skill` 上,`SkillVersion` 没有这个字段(核对了 `protocol/skill.py` 两个类的完整字段列表)。已经加载了 `Skill` 的端点(`get_skill`/`list_versions`/`patch_status`/`export_version`)直接复用;操作 `SkillVersion` 的另外 6 个端点(`add_version`/`get_supporting_file`/`put_supporting_file`/`delete_supporting_file`/`put_prompt`/`get_version`)各加了一次 `store.get_skill(...)`。这比 brief 原文"判定必须发生在加载到 skill 记录之后"暗示的"已经加载"要多做一步,但没有这一步就拿不到 `visibility`,是必要的,不是顺手扩大范围。

对写端点(`add_version`/`put_supporting_file`/`delete_supporting_file`/`put_prompt`/`patch_status`),这次 `get_skill` 调用被放在**真正的存储变更之前**(不只是"返回数据之前")—— 否则一个被拒绝的调用者仍能把一次未授权的写入作为副作用留下(比如 `add_version` 会在 403 之前已经真的插入了一个新版本)。这点 brief 没有明说但逻辑上必须如此,已在代码注释里写明理由。

## 三次变异 —— 实际观察到的输出

全部用临时 `Edit` 改 `skills.py`,跑目标测试,记录输出,再 `Edit` 改回原样,用 `grep -n "MUTATION" skills.py`(exit 1,零匹配)确认改回干净后才继续下一步。

### 变异 1 — `_require_skill_owner_scope` 恒允许

```python
if False and skill.visibility == "agent_private" and not is_admin(principal):  # MUTATION-1
```

跑 `pytest tests/test_skills_api.py -k "test_agent_private_skill_403_for_non_admin_employee or ..."`:

```
FAILED tests/test_skills_api.py::test_agent_private_skill_403_for_non_admin_employee[viewer]
FAILED tests/test_skills_api.py::test_agent_private_skill_403_for_non_admin_employee[operator]
2 failed, 4 passed, 37 deselected
```

失败点是循环里第一个端点 `add_version`:`AssertionError: add_version (viewer): 201 {...}` —— 断言 `403 == 201` 失败。符合预期:正向测试(非 admin 访问 agent_private)红。

### 变异 2(反向)— `_require_skill_owner_scope` 恒拒绝

```python
if True:  # MUTATION-2
    raise HTTPException(status_code=403, detail={...})
```

跑同一批测试:

```
FAILED tests/test_skills_api.py::test_agent_private_skill_admin_not_forbidden
FAILED tests/test_skills_api.py::test_tenant_visibility_skill_unaffected_for_non_admin_employee[viewer]
FAILED tests/test_skills_api.py::test_tenant_visibility_skill_unaffected_for_non_admin_employee[operator]
3 failed, 3 passed
```

`test_agent_private_skill_admin_not_forbidden` 在 `add_version` 上断言 `403 == 201` 失败;`test_tenant_visibility_skill_unaffected_for_non_admin_employee` 两个角色都在 `add_version` 上断言 `403 != 403` 失败(即被恒拒绝命中)。第 2、3 条测试精确地红,第 1 条(agent_private 该 403)和列表测试保持绿 —— 证明测试真在守"没有误伤",不是重言式。

### 变异 3 — 列表过滤摘掉

```python
principal_is_admin = True  # MUTATION-3: filter removed
```

（只改 `list_skills` 内的局部变量,不影响 `_require_skill_owner_scope` 内部独立调用的 `is_admin()`,精确隔离"只测列表过滤"。）

跑同一批测试:

```
FAILED tests/test_skills_api.py::test_list_skills_filters_agent_private_for_non_admin
AssertionError: assert '28b1ac25-...' not in {'107f49c2-...', '28b1ac25-...'}
1 failed, 5 passed
```

只有列表测试红,其余 5 条(2 个 403 参数化 + 1 个 admin-放行 + 2 个 tenant-不受影响参数化)保持绿 —— 证明列表过滤的验证是独立生效的,不是被别的断言顺带撑住。

三次变异后均已改回原样,`grep -n "MUTATION" skills.py` 确认零匹配,`ruff check .` / `ruff format --check .` 通过。

## 测试基线与改后数字

| 文件 | 口径 | 改前 | 改后 |
|---|---|---|---|
| `services/control-plane/tests/test_skills_api.py` | 单文件全量 | 37 passed | 43 passed(+6:2 个 403 测试参数化×2 角色 + 1 个 admin-放行 + 2 个 tenant-不受影响参数化×2 角色 + 1 个列表过滤 = 6) |
| `services/control-plane/tests/test_console_lockdown.py` | 单文件全量 | 96 passed(与 brief 给的基线一致,独立复核确认) | 96 passed(零回归,零新增 —— 本任务未改这个文件) |

两个文件合计跑(`pytest tests/test_skills_api.py tests/test_console_lockdown.py -q`):139 passed(= 43 + 96)。

`ruff check .` 首次跑出 1 个 E501(新测试里一行 108 字符),已拆行修复,复跑通过;`ruff format --check .` 462 个文件 already formatted。`mypy src/control_plane/api/skills.py`(未在 brief 要求内,额外做的低成本 sanity check):`Success: no issues found in 1 source file`。

## 未做但值得记录

- 没有改 `SkillStore` 抽象接口或任何持久化实现(sql.py/memory.py)—— 全程复用已有的 `visibility` 查询参数,符合 brief "如果发现必须改 store 层,停下来报告" 的边界(没有触发这个条件)。
- 没有碰 `console_only()`、`_require_subscribe_role`、PATCH 里既有的 pin/激活高危角色检查 —— 按 brief 第 4 节要求原样保留。
- `POST /v1/skills`、`POST /v1/skills/import`、`POST`/`DELETE /v1/skills/{platform_skill_id}/subscribe` 四条未涉及具体 skill 的端点未动。

## 与 brief 描述不符的真实情况

无实质性不符。唯一补充说明:brief 给的"10 个端点"行号表在 merge 后完全对齐(逐条核对过,`363/449/523/664/735/827/1189/1213/1239/1398` 全部命中对应路由),没有位移。
