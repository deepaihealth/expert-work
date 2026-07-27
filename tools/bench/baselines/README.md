# Entry-chain latency baselines

Output directory for `tools/bench/entry_latency.py`. Each file is one
snapshot from one live-stack run against one agent — see the script's
module docstring for the invocation.

Naming convention: `<date>-<label>.yaml`, e.g. `2026-07-27-before.yaml`
(pre-connection-pooling baseline referenced by Task 5's PR description) /
`2026-07-27-after.yaml`.

`2026-07-27-before.yaml` / `2026-07-27-after.yaml` are the connection-pool
refactor's before/after pair — same agent, same prompt, same environment,
captured either side of the Task 4 connection-pooling change.
