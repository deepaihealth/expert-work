"""_office.to_pdf must raise OfficeTimeout and leave no orphaned soffice process.

Measured inside the real sandbox image (linux/amd64 under QEMU emulation on
an arm64 host, `docker run --entrypoint python`, three runs each unless
noted):
  - tiny docx (single short paragraph): full soffice --convert-to pdf cold
    start ~1.7-1.9s wall clock. That is too close to a 1s timeout (spec
    fallback note: "若沙箱里偶发 1s 内完成,该用例会假失败") to reliably force
    a timeout, so this case converts a large document instead.
  - 300-page docx: ~4.55-4.57s wall clock — turned out to still be too thin
    a margin (fix round 1 finding): that number was measured only under this
    dev machine's QEMU emulation, and `.github/workflows/platform-skills.yml`
    runs this suite on `ubuntu-latest`, which is **native** amd64 with no
    emulation tax. A native run of the same 300-page conversion could
    plausibly finish close to or under the 1s timeout, making
    `check(raised, ...)` below flaky or even vacuously green on CI — the
    exact failure mode the brief's own fallback note exists to avoid.
  - 1500-page docx (this case, current size): ~17.2-17.5s wall clock across
    three runs on this emulated dev machine — the incremental per-page cost
    (~9.5-10ms/page, extrapolated from the 300- vs 1500-page measurements)
    means even a native CI runner several times faster would need to render
    at roughly 1500/15 = 100 pages/second to finish under the 1s timeout,
    which is far outside the range these measurements show. Comfortably
    above the ≥15s target margin requested for this fix.

Real-run finding #1: `soffice` forks a real child, `soffice.bin` (the wrapper,
`oosplash`, does not exec into it), and while running they share one process
group (confirmed with `ps -eo pid,pgid,comm`) — so `os.killpg(...)` does reach
it. Right after the kill, `ps -eo pid,stat,comm` shows `soffice.bin` (and any
gpg helper it had already spawned) in `Z` (zombie) state — dead, just not
reaped, because this harness is PID 1 in the throwaway container and only
reaps its own direct child (`oosplash`), not reparented grandchildren. Plain
`ps -eo comm` still prints "soffice.bin" for an already-dead process, which
would make a naive assertion falsely red — the check below excludes zombie
rows.

Real-run finding #2 (mutation self-proof, spec fallback note: 若不变红,在用例
注释里记录实测结论并改为断言「无 soffice.bin 残留」): temporarily changing
`_office.run_soffice`'s `os.killpg(proc.pid, signal.SIGKILL)` to a plain
`proc.kill()` (SIGKILL to the `oosplash` wrapper PID only, no process-group
signal at all) was tried and did NOT turn this case red — `soffice.bin` (and
its gpg children) still ended up `Z` within the same 2s window. So on this
sandbox image, `soffice.bin`'s lifetime is tied to its direct parent
regardless of whether the signal reaches the whole process group (most
likely LibreOffice's own child processes ask the kernel to be killed when
their parent dies, e.g. via prctl(PR_SET_PDEATHSIG) — not verified beyond
this observed behavior). `os.killpg` therefore cannot be proven necessary by
this specific case on this image/version; it is kept as defense-in-depth
(spec's own stated rationale — "so no orphan soffice keeps the sandbox
busy" — covers soffice versions/launch paths that do not self-monitor their
parent). The assertion below already is the strongest one available here:
it asserts no non-zombie process whose command contains "soffice" remains,
i.e. no live `soffice.bin` residue.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(os.environ["EXPERT_WORK_SKILLS_DIR"]) / "docx" / "scripts"))
import _office
import docx
from _harness import check

os.chdir("/workspace")
d = docx.Document()
for i in range(1500):
    d.add_heading(f"Page {i}", 1)
    d.add_paragraph("Lorem ipsum dolor sit amet. " * 200)
    d.add_page_break()
d.save("t.docx")
raised = False
try:
    _office.to_pdf(Path("t.docx"), Path("to"), timeout=1.0)
except _office.OfficeTimeout:
    raised = True
check(
    raised,
    "expected OfficeTimeout with 1s timeout (cold soffice start + 1500-page render takes ~17s)",
)
time.sleep(2)
ps = subprocess.run(["ps", "-eo", "stat,comm"], capture_output=True, text=True).stdout  # noqa: S607
live = "\n".join(line for line in ps.splitlines() if not line.split(maxsplit=1)[0].startswith("Z"))
check("soffice" not in live, f"soffice left running (non-zombie):\n{ps}")
print("PASS case_shared_office_timeout")
