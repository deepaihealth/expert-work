# Task 3 报告 — knowledge recovery worker 补 gone 守卫(删除卫生 follow-up 打包)

## STATUS: DONE(TDD 先红后绿 + 变异自验已杀)

- 注:本文件覆盖的旧 `task-3-report.md` 是 PR5 遗留(成员页停用并清除入口报告),沿既定覆盖惯例;旧版完整保留在 git 历史。
- 起手 `git merge --ff-only fix-deletion-hygiene-followups`(fast-forward `8d3a71f1..e00dfab3`,带入实施计划文档)。

Commit: `fix(knowledge): recovery worker 补文档已删守卫(与 ingest 路径一致)`

## 做了什么

PR3 只给 ingest 快路径(`ingestion._run`)加了"文档在途被并发删除 → 静默终止"守卫,
recovery worker(`knowledge/recovery.py` 的 `_drive`)同款竞态没加。本项把守卫按
`ingestion.py:216-238` 的形状同构照抄到 recovery 的失败处理分支,做一致性收口。

### 1. 生产代码(1 处,13 行)

`services/control-plane/src/control_plane/knowledge/recovery.py` —— `_drive` 的
`except Exception as exc:` 块开头:

```python
except Exception as exc:
    gone = False
    try:
        gone = (
            await self._store.get_document(
                tenant_id=claim.tenant_id, document_id=claim.document_id
            )
        ) is None
    except Exception:  # noqa: S110  # pragma: no cover - 判定失败按未删处理
        pass
    if gone:
        # 文档已被并发删除 — FK/守卫拒绝写回是正常终止,不算失败。
        logger.debug("knowledge.recovery_document_gone document=%s", claim.document_id)
        return False
    if claim.attempts >= self._max_attempts:
        ...  # 原有逻辑一字未动
```

与参照实现的对应关系(逐字同构):

- `gone = (await get_document(...)) is None`;判定自身失败 → `# noqa: S110` 吞掉,按"未删"处理(继续走原有失败路径)。
- 日志 `logger.debug`,消息 `knowledge.recovery_document_gone document=%s`,**只放 document UUID**,
  无任何请求派生值(filename / error / tenant 都不进日志)。
- 副作用不进 assert:守卫本身不写库,只读 `get_document`。

### 2. 关键决策:守卫放在 except 块开头(支配 mark-failed 路径)

brief 锚点要求"放在会 mark failed 的那条路径上,别改重试计数语义"。选择放在
`except` 首位(而非塞进 `attempts >= max_attempts` 分支内部),理由:

1. **同构**:`ingestion._run` 的守卫就在 `except` 首位,先判 gone 再决定是否 mark failed。
2. **覆盖 mark-failed 路径**:守卫支配(dominates)`mark_document_failed_terminal` 那条分支,锚点要求满足。
3. **不改重试计数语义**:attempts 自增发生在 store 的 CAS claim(`memory.py::_claim` / `sql.py`),
   `_drive` 从不写 attempts;守卫只影响"这一轮失败之后怎么记账",对 claim 计数零影响。
4. **顺带清掉另一半噪音**:文档已删但还有重试余额时,原代码打一条带 stack trace 的
   `knowledge.recovery_retry` WARNING,而那份 lease 到期后根本不会被再 claim(行已不存在)。
   守卫放前面把这条无谓 WARNING 一并消掉——正是本项要收口的"一轮无谓 WARNING"。
   为把这个选择钉住,额外加了一条 caplog 测试,防止后人把守卫窄化进终态分支时静默回归。

返回值取 `False`(不计入 `settled`):`run_once` 契约是 "return how many reached a terminal
state (ready or failed)",被删掉的文档既没 ready 也没 failed,不该进 `knowledge.recovery.settled`。
两个指标计数器(`_recovered` / `_failed_terminal`)在 gone 分支都不 inc,语义一致。

### 3. 测试(`services/control-plane/tests/test_knowledge_recovery.py`,+3 用例)

桩 store 照 `test_knowledge_ingestion.py` 既有形状(`_RecordingStore` 记录
`mark_document_failed_terminal` 调用;子类覆写 `replace_chunks` 制造失败):

| 用例 | 场景 | 断言 |
|---|---|---|
| `test_recovery_document_deleted_mid_flight_is_not_marked_failed` | `_ConcurrentDeleteStore` 写回时删行并抛;`max_attempts=1` → 本轮 claim 即最后一次尝试,**走 mark failed 那条路径** | `failed_terminal == []`、`settled == 0`、行确实已不存在 |
| `test_recovery_failure_with_document_still_present_marks_failed` | 对照分支:`_FailingReplaceStore` 抛异常但文档仍在,attempts 已耗尽 | `settled == 1`、`failed_terminal == [doc]`、状态 FAILED、error 原文透传 |
| `test_recovery_document_deleted_mid_flight_logs_no_retry_warning` | 已删 + 还有重试余额(`max_attempts=5`) | `settled == 0`、`failed_terminal == []`、**零 WARNING 记录** |

`max_attempts=1` 能直达终态分支的依据:in-memory `_is_claimable` 判 `attempts >= max_attempts`
才拒绝,`attempts=0` 的新文档仍可 claim,`_claim` 把 attempts 抬到 1,于是
`claim.attempts(1) >= max_attempts(1)` 成立 → 终态分支。

## 验证记录

1. **Step 2 确认红**(实现前):

   ```
   2 failed, 6 passed
   FAILED test_recovery_document_deleted_mid_flight_is_not_marked_failed
     E  assert [UUID('08d7db...')] == []                    # 旧代码真的 mark 了 failed
   FAILED test_recovery_document_deleted_mid_flight_logs_no_retry_warning
     E  assert ['knowledge.recovery_retry document=674d98f6... attempts=1'] == []
   ```

   对照用例 `..._still_present_marks_failed` 当时即绿(它锁的是既有行为)。

2. **Step 4 确认绿**:`test_knowledge_recovery.py` + `test_knowledge_ingestion.py` → **14 passed**。

3. **Step 5 变异自验**:gone 判定改永假(`if gone:` → `if False:  # MUTANT`,保留 `get_document`
   调用本身,确保杀的是判定而非探测):

   ```
   2 failed, 6 passed
   FAILED test_recovery_document_deleted_mid_flight_is_not_marked_failed
   FAILED test_recovery_document_deleted_mid_flight_logs_no_retry_warning
   WARNING ... knowledge.recovery_failed_terminal document=1fc69c2f... attempts=1
   ```

   变异被两条 gone 测试同时杀死;恢复后 `8 passed`。

4. **回归**:
   - knowledge 全量(`-k knowledge`,非 integration):**80 passed**。
   - control-plane 全量单测:**2112 passed / 6 failed**,6 条全在 `test_eval_engine_live.py`,
     失败原因 `ModuleNotFoundError: No module named 'tools'`——`tools/` 不在
     `[tool.uv.workspace] members` 里,本地 venv 没装(CI 的 pytest 范围含 `tools/eval` 才可导入)。
     已 `git stash` 在**未改动**树上复跑同文件确认同样 6 failed,纯环境问题,与本改动无关
     (改的两个文件都不 import tools)。
   - `ruff format --check` + `ruff check`:两文件全过(`# noqa: S110` 未触发新告警)。
   - mypy:CI 的 mypy 范围不含 `services/control-plane`(`.github/workflows/ci.yml:75`),
     未额外跑单文件 mypy(单文件 mypy 假阳率高)。

## Concerns

1. **(无阻塞,唯一裁量点)守卫位置**:选了 except 首位并用第 3 条 caplog 测试钉住。
   若终审认为"只该消 FAILED 噪音、retry WARNING 要留着当可观测信号",把守卫移进
   `attempts >= max_attempts` 分支即可,同时删掉第 3 条测试。
2. **(无阻塞)`claim.content is None` 那条腿没加守卫**(legacy 无留存字节 → 直接 mark failed)。
   那条路径同样能撞"claim 之后行被删"的窄窗,但 brief 只点名失败分支,且
   `mark_document_failed_terminal` 对已删行 0 命中(良性)。按外科手术式改动原则未扩面,显式记一笔。
3. **(无阻塞)SQL store 未做真栈集成测**:gone 判定只依赖 `get_document` 返回 None,
   in-memory 与 SQL 两实现语义一致("行不在就返 None"),且 PR3 的 ingest 侧守卫同样只有
   in-memory 覆盖,与既有做法持平。
