# Task 2 Report — `fallbackAnswer` 收全该轮所有助手消息(保真)

## STATUS

DONE — TDD 先红后绿,5 测全绿(+ PlaygroundTab 45 测无回归),变异自验通过,已提交。

## Commit

- `fix(admin-ui): 历史轮 fallbackAnswer 收全该轮助手消息(replay 失败时不丢内容)`

## 变更文件

- `apps/admin-ui/src/pages/agent_detail/playground/history_turns.ts`
- `apps/admin-ui/src/pages/agent_detail/playground/__tests__/history_turns.test.ts`

## 改动前后 `answer` 收集逻辑对照

**改动前**(只取紧邻下一条):
```ts
const next = messages[i + 1];
const answer = next && next.role === "assistant" ? next.content : "";
pairs.push({ input: m.content, answer });
```
一个 user 消息后若有多条 assistant 消息(多步 run 常见,实测截图那轮 7 条),只有第 1 条被采到,其余全部丢失。

**改动后**(收全该轮所有 assistant 消息):
```ts
const answers: string[] = [];
for (let j = i + 1; j < messages.length && messages[j].role !== "user"; j += 1) {
  answers.push(messages[j].content);
}
pairs.push({ input: m.content, answer: answers.join("\n\n") });
```
从当前 user 消息之后开始扫描,收集所有 `role !== "user"` 的消息(即该轮内的全部 assistant 消息),直到遇到下一条 `role === "user"` 或消息列表结束为止,用 `"\n\n"` 拼接。

**一字未动的部分**(按 brief 边界要求逐项核对):
- `pairs.length !== runs.length → null` 判据:整个 for 循环外层判据未改一个字符。
- `is_resume` 被忽略的语义:未引用、未使用,原样保留。
- docstring 补充说明改在原注释块末尾新增一段,原有三段(配对方式/is_resume 语义/count mismatch 降级)逐字未动。
- `HistoryTurn` 接口、返回结构、`key`/`runId`/`status` 字段全部未动。

## 红/绿实际输出

**RED**(Step 1 新增的保真测试,实现前):
```
❯ src/pages/agent_detail/playground/__tests__/history_turns.test.ts (5 tests | 1 failed)
  × collects ALL assistant messages in a turn (a run can emit several), joined by blank lines
AssertionError: expected [ …(2) ] to deeply equal [ …(2) ]
- Expected
+ Received
@@ -1,12 +1,8 @@
   [
     {
-      "fallbackAnswer": "a1\n\na2\n\na3",
+      "fallbackAnswer": "a1",
...
 Test Files  1 failed (1)
      Tests  1 failed | 4 passed (5)
```
既有 4 个用例(顺序配对/count mismatch→null/尾轮无回复空 fallback/空线程→[])全绿,证实新增测试确实新增了未覆盖行为,而非破坏既有判据。

**GREEN**(Step 3 实现后,Step 4):
```
$ vitest run src/pages/agent_detail/playground/__tests__/history_turns.test.ts src/pages/__tests__/PlaygroundTab.test.tsx
 Test Files  2 passed (2)
      Tests  50 passed (50)
   Duration  10.21s
```
history_turns.test.ts 5/5 通过;PlaygroundTab.test.tsx 45/45 通过(全量,未过滤用例)。

`pnpm typecheck`(`tsc -b --noEmit`):无输出,exit 0,干净通过。

## PlaygroundTab.test.tsx 未受波及的证据

- 全量跑(未加 `-t` 过滤),45 个测试全部通过,含 brief/global-constraints 点名的 `:1675-1930` 区间「history lazy rebuild on resume」5 个用例(该区间用例全部在 45 之内一起绿)。
- `git diff` 确认改动只涉及 `history_turns.ts` + 其测试文件两个,`PlaygroundTab.tsx` 零改动(命中"不碰该并行任务正在搬迁的文件"边界)。
- `fallbackAnswer` 在 `PlaygroundTab.tsx` 内只作为字符串传给 `MarkdownView` 渲染(`fallbackAnswer={h.fallbackAnswer}` → `<MarkdownView>{fallbackAnswer}</MarkdownView>`),类型契约(`string`)未变,只是内容可能变长——不改变任何调用点的类型或分支逻辑。

## 变异自验(brief Step 5)

操作:把实现改回"只取紧邻下一条"(即恢复到改动前的 3 行逻辑),其余不动。

```
$ vitest run src/pages/agent_detail/playground/__tests__/history_turns.test.ts
 ❯ ... (5 tests | 1 failed)
     × collects ALL assistant messages in a turn (a run can emit several), joined by blank lines
AssertionError: expected [ …(2) ] to deeply equal [ …(2) ]
-     "fallbackAnswer": "a1\n\na2\n\na3",
+     "fallbackAnswer": "a1",
 Test Files  1 failed (1)
      Tests  1 failed | 4 passed (5)
```
保真测试如预期变红(与 Step 1 的原始 RED 报错完全一致,证明该测试确实压住了"只取下一条"这个 mutant),其余 4 个既有用例保持绿(证明 mutant 不影响配对判据本身)。随后恢复正确实现,重新跑确认全绿(5/5 + PlaygroundTab 45/45),`git diff` 确认最终文件与恢复前一致、无残留改动。

## 实现备注 / concerns

1. `HistoryMessage.role` 类型是 `"user" | "assistant"`(`apps/admin-ui/src/api/sessions.ts:115`),没有第三种角色,所以内层循环用 `messages[j].role !== "user"` 等价于 `=== "assistant"`,选用前者是因为语义更贴合 docstring("直到下一条 user 之前的全部消息"),两种写法在当前类型下行为完全一致。
2. worktree 首次运行缺 `node_modules`(全新 worktree,未预装),已在 `apps/admin-ui` 内 `pnpm install`(锁文件未变,resolution 跳过,472 包全部命中缓存,无版本漂移风险)。
3. 未触碰 `TurnCard`/`PlaygroundTab.tsx`,与并行搬迁任务文件集互不相交。
