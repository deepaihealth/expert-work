# Volume restore runbook (Stream J.15-补强-2)

> Recovery procedure when a user's `/workspace` docker volume is lost
> (host failure / accidental `docker volume rm` / disk corruption). This
> is the operator-facing path; the supervisor's archive + daily backup
> pipelines (`services/sandbox-supervisor/src/sandbox_supervisor/lifecycle.py`)
> drop the source artifacts into ObjectStore for this script to pull.
>
> Drill: see `packages/expert-work-persistence/tests/test_volume_restore_drill.py`
> for the testcontainers integration test that exercises the same
> library calls end-to-end.

## When to use this

| Symptom | Action |
|---------|--------|
| User reports lost files in a long-running session | Pick the latest J-29 daily backup for the date the files were last known good |
| Host filesystem corruption / `docker volume rm` accident | Pick the most recent J-29 daily backup *or* the J-36 archive (whichever is fresher) |
| Recovering an explicitly soft-deleted workspace (user changed their mind) | Pull the J-36 archive — same data as the day the user deleted |

The supervisor stores artifacts in two prefixes:

* **J-36 archive**: `volume-archive/{tenant_id}/{user_id}/{volume_name}.tar.gz`
  — physical copy taken when a workspace is soft-deleted (Mini-ADR J-36
  lifecycle 第 3 档). Latest content; one per workspace lifetime.
* **J-29 daily backup**: `volume-backups/{tenant_id}/{user_id}/{YYYY-MM-DD}/{volume_name}.tar.gz`
  — daily off-peak snapshot of every active workspace. Retention =
  `workspace_backup_retention_days` (default 7 days).

## Pre-flight

1. Confirm operator has access to the ObjectStore credentials used by
   the supervisor (the `EXPERT_WORK_SANDBOX_OBJECT_STORE_*` env vars).
2. Confirm docker is reachable on the target host (`docker info`).
3. Confirm the target Postgres is reachable (for the post-restore
   `UPDATE user_workspace SET volume_name = ...`).
4. Set tenant + user UUIDs:

   ```sh
   export TENANT=...
   export USER=...
   ```

## Step 1 — find the artifact

```sh
# List archive for this user (J-36; usually just one if a soft-delete ran):
aws s3 ls "s3://expert-work-volume-backups/volume-archive/$TENANT/$USER/"

# Or list daily backups (J-29 第 2 项):
aws s3 ls --recursive "s3://expert-work-volume-backups/volume-backups/$TENANT/$USER/"
```

For MinIO / Aliyun OSS replace with the matching CLI; the keys are
ObjectStore-backend agnostic.

## Step 2 — run the restore script

```sh
# Latest available (prefer archive, fall back to most recent backup):
python -m tools.persistence.restore_volume \
    --tenant "$TENANT" \
    --user   "$USER"

# Specific backup date (J-29 第 2 项, YYYY-MM-DD):
python -m tools.persistence.restore_volume \
    --tenant "$TENANT" \
    --user   "$USER" \
    --date   2026-05-21

# Custom suffix (multiple restores on the same volume):
python -m tools.persistence.restore_volume \
    --tenant "$TENANT" \
    --user   "$USER" \
    --suffix "2026-05-21-ticket-1234"
```

The script:

1. Lists the prefer-archive-then-backup keys for this user.
2. Pulls the bytes from ObjectStore.
3. Runs `docker volume create {original_name}_restored_{suffix}`.
4. Pipes the tar.gz into a throwaway hardened container that extracts
   into `/ws` of the new volume.
5. Prints the **new** volume name. **It does not auto-promote.**

## Step 3 — verify the restored volume

```sh
# Inspect contents from a throwaway container:
docker run --rm \
    --read-only \
    -v "${RESTORED_VOLUME}:/ws:ro" \
    expert-work-sandbox:dev \
    ls -la /ws
```

Operator sanity checks:

* Files the user reported as lost are present.
* Permissions / ownership match the original volume.
* Total size is close to the source (`docker exec ... du -sb /ws`).

## Step 4 — promote the volume (operator decision)

If the restored volume looks right, point the workspace row at it. **Do
not** simply rename — `volume_name` is the source of truth used by the
runtime provider on the next `acquire()`.

```sql
-- The restored volume becomes the new live volume for this workspace.
UPDATE user_workspace
SET    volume_name      = '{RESTORED_VOLUME}',
       last_accessed_at = NOW()
WHERE  tenant_id = '{TENANT}'::uuid
  AND  user_id   = '{USER}'::uuid;
```

The supervisor's next `acquire()` will mount the new volume; the user's
session restores transparently.

If the restored volume is **wrong** (operator discards):

```sh
docker volume rm "${RESTORED_VOLUME}"
```

The original volume row is untouched.

## Step 5 — record the drill

If this restore was triggered by a routine DR drill rather than a real
incident, log it in the DR drill tracker (`backup_record` row +
`dr_drill` table — see `docs/dr/RUNBOOK.md`).

## Failure modes

| Error | Likely cause | Mitigation |
|-------|-------------|------------|
| `no archive or backup found ...` | The artifact never landed (DLQ failure) | Check `volume_backup_dlq` table; manual replay if needed |
| `subprocess.CalledProcessError ... tar -xzf`, with `Cannot change ownership to uid 10000` | The extract container is missing `CAP_CHOWN`/`CAP_FOWNER` — a regression in `_hydrate_volume_with_docker`'s capability set | Not an archive problem. The tree lands complete with correct modes but owned by `root:root`; check the `stat` owner column, not for missing files. See that function's docstring and `test_hydrate_preserves_non_root_ownership_and_modes` |
| `subprocess.CalledProcessError ... tar -xzf`, other errors | Source archive corrupted | Try the prior day's J-29 backup |
| `ObjectStoreError` on pull | Credentials / endpoint mismatch | Re-check `EXPERT_WORK_SANDBOX_OBJECT_STORE_*` env |

**Re-running is safe.** The volume name is deterministic (`<volume>_restored_<suffix>`, suffix defaults to `manual`) and `docker volume create` exits 0 on an existing name, so a second run extracts over whatever the first run left behind. That path is covered by the drill test's second pass — it is the reason the extract container keeps `DAC_OVERRIDE` (on a half-restored volume the directories are already `10000:10000 0700`, and root is no longer their owner). Pass `--suffix` if you want a side-by-side volume instead of overwriting.

> A row previously listed here claimed `docker volume create` raises on a name collision. It does not — it exits 0. Removed rather than reworded: nothing raises there.

## Related

* [J.15-补强-1 lifecycle states (Mini-ADR J-36)](../streams/STREAM-J-DESIGN.md#9-j15-有状态-per-user-执行环境)
* [J.15-补强-2 backup pipeline design (§ 9.5.2)](../streams/STREAM-J-DESIGN.md#95-m0-补强-j-29--j-36-生产级数据保护--lifecycle)
* [Audit restore (K14 sibling pattern)](./audit-restore.md)
* [Postgres restore (K15)](./pg-restore.md)
