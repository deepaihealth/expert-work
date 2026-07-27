# Entry-chain latency baselines

Output directory for `tools/bench/entry_latency.py`. Each file is one
snapshot from one live-stack run against one agent — see the script's
module docstring for the invocation.

Naming convention: `<date>-<label>.yaml`, e.g. `2026-07-27-before.yaml`
(pre-connection-pooling baseline referenced by Task 5's PR description) /
`2026-07-27-after.yaml`.

No baseline file is committed here yet — Task 4 (this script) intentionally
does not run against a live stack (see `.superpowers/sdd/perf-task-4-report.md`
for why); the first real baseline is captured separately.
