"""``Idempotency-Key`` support for the external run endpoint (P2-a Task 13).

The fingerprint hashes the **canonical request body plus the agent code**,
not the ``Idempotency-Key`` header value itself. Storing a digest (rather
than trusting the key alone) is what lets the endpoint tell a genuine retry
apart from a *reused* key: if a third party changes ``input`` but forgets to
mint a new key, a key-only implementation would silently return the old
run's result — the caller thinks it dispatched new work and gets back a
stale answer with no signal anything is wrong.

``agent_code`` is folded into the digest for the same reason, one level up
(P2-a Task 13, review finding — the original brief's ``request_digest``
covered the body only). ``agent_code`` lives in the URL path, never in the
request body, and the partial unique index backing this feature is scoped
to ``(tenant_id, idempotency_key)`` only — it has no idea which agent a key
was minted against. Without ``agent_code`` in the digest, a third party
could call ``POST /v1/agents/agent-A/runs`` with key ``order-8899``, then
later call ``POST /v1/agents/agent-B/runs`` with the *same* key and the
*same* body: the digest would match, the endpoint would conclude "this is a
retry", and it would silently hand back agent-A's run as if it were
agent-B's — the caller believes it dispatched work to agent-B and receives
agent-A's result with no error at all. Folding ``agent_code`` into the
digest turns that into a loud ``422 IDEMPOTENCY_KEY_REUSED`` instead of a
silent cross-agent mix-up. Do not drop this parameter to "simplify" the
signature — it is the only thing standing between a key collision and a
misrouted result.
"""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel

IDEMPOTENCY_HEADER = "Idempotency-Key"
MAX_IDEMPOTENCY_KEY_LEN = 255

__all__ = ["IDEMPOTENCY_HEADER", "MAX_IDEMPOTENCY_KEY_LEN", "request_digest"]


def request_digest(payload: BaseModel, *, agent_code: str) -> str:
    """sha256 of the canonicalised request body, salted with ``agent_code``.

    The body is dumped with sorted keys / no ASCII escaping / compact
    separators so two structurally-identical payloads always hash the same
    regardless of field-declaration order. ``agent_code`` is prefixed with a
    NUL separator before hashing (see module docstring for why it must be
    included at all) — the separator byte can never appear inside the JSON
    dump, so there is no ambiguity between ``(agent_code, body)`` pairs that
    would otherwise concatenate to the same string.
    """
    body = payload.model_dump(mode="json")
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    salted = f"{agent_code}\x00{canonical}"
    return hashlib.sha256(salted.encode("utf-8")).hexdigest()
