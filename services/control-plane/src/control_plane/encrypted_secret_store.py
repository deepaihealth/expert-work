"""Encrypted-secret vault — Stream Q (Mini-ADR Q-1/Q-2/Q-7).

B-286 —— the implementation moved to :mod:`expert_work.runtime.secret_store.sql_encrypted` so the
retention-cleanup job can resolve the same ``secret://`` refs; this module keeps the old
import path.
"""

from __future__ import annotations

from expert_work.runtime.secret_store.sql_encrypted import (
    ENV_KEK_VERSION as ENV_KEK_VERSION,
)
from expert_work.runtime.secret_store.sql_encrypted import (
    EnvelopeCipher as EnvelopeCipher,
)
from expert_work.runtime.secret_store.sql_encrypted import (
    SqlEncryptedSecretStore as SqlEncryptedSecretStore,
)
from expert_work.runtime.secret_store.sql_encrypted import (
    build_kek_from_b64 as build_kek_from_b64,
)

__all__ = ["ENV_KEK_VERSION", "EnvelopeCipher", "SqlEncryptedSecretStore", "build_kek_from_b64"]
