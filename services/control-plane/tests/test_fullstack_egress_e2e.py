"""Full-stack egress e2e — Stream I.1b, test matrix #60.

Exercises the whole credential-egress chain end to end:

    ExecPythonTool -> sandbox-supervisor -> sandbox container
        -> credential-proxy -> mock-upstream

A composed sub-stack (postgres + migrate + credential-proxy +
sandbox-supervisor + mock-upstream) is brought up by a module fixture;
the test seeds a ``secret_allowlist`` row, drives ``ExecPythonTool``
directly — no LLM, see STREAM-I-DESIGN § 4.1 — and asserts the proxy
injected the secret and audited the call without leaking the value.

The fixture is *module*-scoped (not session): it tears the stack down
before the F.8 ``test_supervisor_integration`` module runs, so the two
do not both hold the ``expert-work-sandbox-egress`` network at once.

Skips when Docker / compose is unavailable, so the unit ``pytest`` job
(``-m "not integration"``) never touches Docker.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import asyncpg
import pytest

from orchestrator.tools import ExecPythonTool, HTTPSupervisorRuntime, ToolContext

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

#: ``infra/`` — compose file + the sandbox-image build context live here.
_INFRA = Path(__file__).resolve().parents[3] / "infra"
_COMPOSE_FILE = _INFRA / "docker-compose.yml"
#: The composed supervisor launches sandboxes from this tag (its default).
_SANDBOX_IMAGE = "expert-work-sandbox:dev"
#: Service images the sub-stack consumes — ``migrate`` reuses the
#: control-plane image, hence ``control-plane-blue`` (its sole builder,
#: Stream I.2) is built even though control-plane is not in the #60 chain.
_BUILD_SERVICES = ("control-plane-blue", "sandbox-supervisor", "credential-proxy")
#: Services the #60 chain needs — control-plane / redis are not in it.
_STACK_SERVICES = (
    "postgres",
    "migrate",
    "credential-proxy",
    "sandbox-supervisor",
    "mock-upstream",
)
#: sandbox-supervisor's host-mapped port (docker-compose.yml).
_SUPERVISOR_URL = "http://localhost:8001"
#: Host-mapped Postgres — seed the allowlist / read the audit table.
_DB_DSN = "postgresql://expert_work:expert_work_dev@localhost:5432/expert_work_dev"

#: Must match infra/credential-proxy/secrets.env.
_SECRET_REF = "e2e-upstream-token"
_SECRET_VALUE = "dev-e2e-upstream-token-not-a-real-secret"
_AGENT_NAME = "e2e-agent"
_AGENT_VERSION = "1"

#: Python the sandbox runs — stdlib only (the sandbox image has no pip).
#: ``__TENANT_ID__`` is substituted per run. It POSTs through the
#: credential-proxy, which injects the secret and forwards to
#: mock-upstream; mock-upstream echoes the request back.
_SANDBOX_CODE = """\
import urllib.request

req = urllib.request.Request(
    "http://credential-proxy:8080/forward",
    method="POST",
    data=b'{"probe": "i1b-e2e"}',
    headers={
        "X-Expert-Work-Tenant": "__TENANT_ID__",
        "X-Expert-Work-Agent": "e2e-agent",
        "X-Expert-Work-Agent-Version": "1",
        "X-Expert-Work-Secret-Ref": "e2e-upstream-token",
        "X-Expert-Work-Upstream": "http://mock-upstream:9100/echo",
        "Content-Type": "application/json",
    },
)
with urllib.request.urlopen(req, timeout=15) as resp:
    print("UPSTREAM-STATUS:" + str(resp.status))
    print(resp.read().decode())
"""


#: 单条 ``docker`` 命令的墙钟上限。没有它,一次卡住的拉取会一直等到
#: GitHub 把整个 job 砍掉(``timeout-minutes: 40``),日志里只留
#: ``The operation was canceled``——不指向任何一条测试,也没有 stderr。
#: 2026-08-28 一天烧掉三次 40 分钟就是这么来的(main push run 一次、PR 两
#: 次),而 integration job 的 pytest 不像 unit job 带 ``--timeout``,
#: 没有第二道闸。取值按「正常值的数倍」定:本模块整套在 CI 上跑完约 5
#: 分钟,所以 build 给 600s 已经很宽,超了就是真卡住而不是慢。
_DOCKER_TIMEOUT_S = 180.0
#: 镜像构建档——要拉基础镜像 + 装依赖,比其余命令慢一个量级。
_DOCKER_BUILD_TIMEOUT_S = 600.0
#: ``compose up`` 档:内层已有 ``--wait-timeout 200``,这是它卡在
#: 拉镜像(还没进入 wait)时的外层兜底。
_DOCKER_UP_TIMEOUT_S = 420.0


def _docker(*args: str, timeout: float = _DOCKER_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    """Run a ``docker`` CLI command — a test-harness helper.

    ``timeout`` 超时不是让它静静挂着,而是抛一条点名到子命令的
    ``AssertionError``,让 CI 日志直接说清「哪条 docker 命令卡了多久」。

    这里用 ``raise`` 而不是 :func:`pytest.fail`:两者在 pytest 里都判失败,
    但 ``pytest.fail`` 的 ``NoReturn`` 语义 CodeQL 读不出来,会把 except 分支
    当成 fall through(隐式返回 ``None``),报「explicit returns mixed with
    implicit returns」。显式 ``raise`` 让两条出路都是显式的。
    """
    try:
        return subprocess.run(  # noqa: S603 — fixed argv, no shell, test harness only
            ["docker", *args],  # noqa: S607 — ``docker`` is on PATH in CI and dev
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        message = (
            f"`docker {' '.join(args)}` 超过 {timeout:.0f}s 未返回 —— "
            "多半是镜像拉取被限流/卡死(Docker Hub 在共享 runner IP 上限流是老问题)。"
        )
        raise AssertionError(message) from exc


def _compose(*args: str, timeout: float = _DOCKER_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    """Run a ``docker compose`` command against the infra compose file."""
    return _docker("compose", "-f", str(_COMPOSE_FILE), *args, timeout=timeout)


def _compose_down() -> None:
    """Tear the sub-stack down.

    ``docker compose down`` only removes services in the *active* profile
    set — without these flags the profile-gated services (credential-proxy
    / sandbox-supervisor / mock-upstream) survive. ``-v`` is omitted on
    purpose: the postgres-data volume is the shared local dev volume.
    """
    _compose("--profile", "full", "--profile", "e2e", "down", "--remove-orphans")


@pytest.fixture(scope="module", autouse=True)
def _egress_stack() -> Iterator[None]:
    """Build images + bring up the #60 egress sub-stack.

    Module-scoped so the stack — and the ``expert-work-sandbox-egress``
    network — is gone before the F.8 integration module runs.
    """
    probe = _docker("version", "--format", "{{.Server.Version}}")
    if probe.returncode != 0:
        pytest.skip("docker daemon unavailable")

    built = _docker(
        "build",
        "-t",
        _SANDBOX_IMAGE,
        str(_INFRA / "sandbox-image"),
        timeout=_DOCKER_BUILD_TIMEOUT_S,
    )
    if built.returncode != 0:
        pytest.fail(f"sandbox image build failed:\n{built.stderr[-600:]}")

    images = _compose("build", *_BUILD_SERVICES, timeout=_DOCKER_BUILD_TIMEOUT_S)
    if images.returncode != 0:
        pytest.fail(f"service image build failed:\n{images.stderr[-600:]}")

    up = _compose(
        "up",
        "-d",
        "--wait",
        "--wait-timeout",
        "200",
        *_STACK_SERVICES,
        timeout=_DOCKER_UP_TIMEOUT_S,
    )
    if up.returncode != 0:
        logs = _compose("logs", "--tail", "60").stdout
        _compose_down()
        pytest.fail(f"egress stack failed to start:\n{up.stderr[-400:]}\n{logs[-1200:]}")

    yield

    _compose_down()


async def _seed_allowlist(tenant_id: uuid.UUID) -> None:
    """Insert the ``secret_allowlist`` row the proxy checks before injecting."""
    conn = await asyncpg.connect(_DB_DSN)
    try:
        await conn.execute(
            "INSERT INTO secret_allowlist "
            "(tenant_id, agent_name, agent_version, secret_ref, purpose) "
            "VALUES ($1, $2, $3, $4, $5)",
            tenant_id,
            _AGENT_NAME,
            _AGENT_VERSION,
            _SECRET_REF,
            "i1b-e2e",
        )
    finally:
        await conn.close()


async def _fetch_audit(tenant_id: uuid.UUID) -> list[asyncpg.Record]:
    """Read the proxy's audit rows for the run's tenant."""
    conn = await asyncpg.connect(_DB_DSN)
    try:
        return await conn.fetch(
            "SELECT secret_ref, target_host, status, inject_kind, error_msg "
            "FROM credential_proxy_audit WHERE tenant_id = $1",
            tenant_id,
        )
    finally:
        await conn.close()


def _extract_echo(content: str) -> dict[str, Any]:
    """Pull mock-upstream's echoed JSON document out of the tool output."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("{"):
            return json.loads(stripped)
    raise AssertionError(f"no echo JSON in tool output:\n{content}")


async def test_gate_60_fullstack_egress_injects_and_audits() -> None:
    """#60 — exec_python → sandbox → credential-proxy → mock-upstream.

    The proxy injects the allow-listed secret as a bearer token and
    audits the call with the ref + host + status, never the value.
    """
    tenant_id = uuid.uuid4()
    await _seed_allowlist(tenant_id)

    tool = ExecPythonTool(client=HTTPSupervisorRuntime(base_url=_SUPERVISOR_URL))
    result = await tool.call(
        {"code": _SANDBOX_CODE.replace("__TENANT_ID__", str(tenant_id)), "timeout_s": 30},
        ctx=ToolContext(tenant_id=tenant_id),
    )

    # The sandbox reached mock-upstream through the proxy.
    assert result.meta["exit_code"] == 0, result.content
    assert "UPSTREAM-STATUS:200" in result.content
    echo = _extract_echo(result.content)
    assert echo["path"] == "/echo"
    # The proxy injected the resolved secret as an Authorization header.
    assert echo["headers"].get("authorization") == f"Bearer {_SECRET_VALUE}"

    # The proxy audited the injection — ref + host + status, no value.
    rows = await _fetch_audit(tenant_id)
    assert len(rows) == 1
    audit = rows[0]
    assert audit["secret_ref"] == _SECRET_REF
    assert audit["target_host"] == "mock-upstream"
    assert audit["status"] == "ok"
    assert audit["error_msg"] is None
    # The plaintext secret value never lands in any audited field.
    assert _SECRET_VALUE not in " ".join(str(v) for v in audit.values())
