# 测评报告

对外和汇报用的测评报告归档。每次测评一个目录，固定三个文件，文件名 = `expert-work-<测评集>-<YYYY-MM-DD>`，单独发出去也看得出是什么：

- `.html`：报告原始页面，浏览器直接打开
- `.png`：长图（宽 1242px）
- `.pdf`：PDF（单页长版，与长图一致）

## 目录约定

```
docs/eval-reports/
  <维度>/                        harness、knowledge-base、memory …
    <YYYY-MM-DD>-<测评集>/        测评完成日期 + 测评集名，小写、连字符
      expert-work-<测评集>-<YYYY-MM-DD>.html
      expert-work-<测评集>-<YYYY-MM-DD>.png
      expert-work-<测评集>-<YYYY-MM-DD>.pdf
```

例：`harness/2026-10-17-harness-bench/expert-work-harness-bench-2026-10-17.pdf`

- 同一维度、同一测评集再测一次：按日期新开目录，不覆盖旧的。
- 新的测评维度：新建 `<维度>/` 目录，并在下方索引加一行。
- 和公开榜单对比时，先确认官方成绩用的是哪一版评分程序，我方用同一版本打分。
- 导出方式：无头 Chrome，宽度 1242px，等字体加载完成后截整页长图、打印单页 PDF。

## 索引

| 日期 | 维度 | 测评集 | 结论 | 目录 |
|---|---|---|---|---|
| 2026-10-17 | harness | Harness-Bench（257 题，DeepSeek V4 Pro，4 轮） | 4 轮平均 68.44，8 家中第 2（第 1 名 Codex 69.25） | [harness/2026-10-17-harness-bench](harness/2026-10-17-harness-bench/) |
