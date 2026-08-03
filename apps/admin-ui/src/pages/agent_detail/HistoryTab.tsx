/**
 * History tab — Stream HX-5 PR 2.
 *
 * Revision history of the manifest (``agent_spec_revision``): a table
 * of immutable snapshots, a Monaco diff between any two of them, and a
 * rollback action. Rollback rolls *forward* — it appends a new revision
 * carrying the old snapshot's content (Mini-ADR HX-E2); history is
 * never rewritten, so the table only ever grows.
 *
 * Diff semantics: selecting two rows compares older → newer. The
 * snapshots are fetched lazily per selection (the list endpoint
 * returns summaries only) and rendered as YAML through the same
 * ``js-yaml`` dump the Manifest tab uses, so the diff matches what an
 * operator sees in the editor.
 */
import { useCallback, useEffect, useMemo, useState, type Key } from "react";
import { Alert, Button, Card, Popconfirm, Space, Table, Tag, Typography } from "antd";
import { DiffEditor } from "@monaco-editor/react";
import { dump as yamlDump } from "js-yaml";
import { History, Undo2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import { ApiError } from "../../api/client";
import {
  getRevision,
  listRevisions,
  rollbackToRevision,
  type AgentDetailResponse,
  type RevisionSummary,
} from "../../api/agents";
import { ReadonlyTooltip } from "../../components/ReadonlyTooltip";
import { concreteTenantScope, useTenantScope } from "../../tenant/TenantScopeContext";
import { useIsTenantSwitched } from "../../tenant/useIsTenantSwitched";

const { Text } = Typography;

interface HistoryTabProps {
  detail: AgentDetailResponse;
  /** Called after a successful rollback — parent refetches the record
   *  so the header SHA / updated_at reflect the new current state. */
  onRolledBack: () => void;
}

export function HistoryTab({ detail, onRolledBack }: HistoryTabProps) {
  const { t } = useTranslation();
  const { name, version } = detail.record;
  // Track C W2 — 切入态读透传:revision 读接口带 ?tenant_id=(组件内直取);
  // 回滚是写操作,切入态置灰。
  const { apiTenantScope } = useTenantScope();
  const isTenantSwitched = useIsTenantSwitched();

  const [items, setItems] = useState<RevisionSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<number[]>([]);
  const [diff, setDiff] = useState<{ older: string; newer: string; label: string } | null>(null);
  const [diffLoading, setDiffLoading] = useState(false);
  const [rollingBack, setRollingBack] = useState<number | null>(null);

  const currentSha = detail.record.spec_sha256;

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await listRevisions(name, version, concreteTenantScope(apiTenantScope));
      setItems(result.items);
    } catch (err) {
      setError(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err));
    } finally {
      setLoading(false);
    }
  }, [name, version, apiTenantScope]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const loadDiff = useCallback(
    async (pair: number[]) => {
      const [a, b] = [...pair].sort((x, y) => x - y);
      setDiffLoading(true);
      setError(null);
      try {
        const [older, newer] = await Promise.all([
          getRevision(name, version, a, concreteTenantScope(apiTenantScope)),
          getRevision(name, version, b, concreteTenantScope(apiTenantScope)),
        ]);
        setDiff({
          older: yamlDump(older.record.spec, { lineWidth: 120 }),
          newer: yamlDump(newer.record.spec, { lineWidth: 120 }),
          label: t("history_tab.diff_label", { older: a, newer: b }),
        });
      } catch (err) {
        setError(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err));
      } finally {
        setDiffLoading(false);
      }
    },
    [name, version, t, apiTenantScope],
  );

  const handleSelect = useCallback(
    (keys: Key[]) => {
      // Keep at most two selections: the newest pick replaces the older
      // of the previous pair, so comparing is always one click away.
      const next = keys.map(Number).slice(-2);
      setSelected(next);
      if (next.length === 2) {
        void loadDiff(next);
      } else {
        setDiff(null);
      }
    },
    [loadDiff],
  );

  const handleRollback = useCallback(
    async (revision: number) => {
      setRollingBack(revision);
      setError(null);
      try {
        await rollbackToRevision(name, version, revision);
        setSelected([]);
        setDiff(null);
        await refresh();
        onRolledBack();
      } catch (err) {
        setError(err instanceof ApiError ? `${err.code}: ${err.message}` : String(err));
      } finally {
        setRollingBack(null);
      }
    },
    [name, version, refresh, onRolledBack],
  );

  const columns = useMemo(
    () => [
      {
        title: t("history_tab.col_revision"),
        dataIndex: "revision",
        width: 110,
        render: (revision: number, row: RevisionSummary) => (
          <Space size={8}>
            <Text strong className="mono">
              #{revision}
            </Text>
            {row.spec_sha256 === currentSha && (
              <Tag color="success" bordered={false}>
                {t("history_tab.current")}
              </Tag>
            )}
          </Space>
        ),
      },
      {
        title: t("history_tab.col_sha"),
        dataIndex: "spec_sha256",
        render: (sha: string) => (
          <Text code style={{ fontSize: 12 }}>
            {sha.slice(0, 12)}…
          </Text>
        ),
      },
      {
        title: t("history_tab.col_actor"),
        dataIndex: "actor_id",
        ellipsis: true,
      },
      {
        title: t("history_tab.col_time"),
        dataIndex: "created_at",
        width: 200,
        render: (ts: string) => new Date(ts).toLocaleString(),
      },
      {
        title: "",
        key: "actions",
        width: 130,
        render: (_: unknown, row: RevisionSummary) =>
          row.spec_sha256 === currentSha ? null : (
            <ReadonlyTooltip on={isTenantSwitched}>
              <Popconfirm
                title={t("history_tab.rollback_confirm_title", { revision: row.revision })}
                description={t("history_tab.rollback_confirm_body")}
                onConfirm={() => void handleRollback(row.revision)}
                okText={t("history_tab.rollback")}
                disabled={isTenantSwitched}
              >
                <Button
                  size="small"
                  icon={<Undo2 size={13} strokeWidth={1.5} />}
                  loading={rollingBack === row.revision}
                  disabled={isTenantSwitched}
                  data-testid={`history-rollback-${row.revision}`}
                >
                  {t("history_tab.rollback")}
                </Button>
              </Popconfirm>
            </ReadonlyTooltip>
          ),
      },
    ],
    [t, currentSha, rollingBack, handleRollback, isTenantSwitched],
  );

  return (
    <div data-testid="history-tab-root">
      <Card
        title={
          <Space size={8}>
            <History size={15} strokeWidth={1.5} />
            {t("history_tab.title")}
          </Space>
        }
        extra={
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("history_tab.select_hint")}
          </Text>
        }
      >
        {error !== null && (
          <Alert
            type="error"
            showIcon
            message={error}
            style={{ marginBottom: 12 }}
            data-testid="history-tab-error"
          />
        )}
        <Table<RevisionSummary>
          size="small"
          rowKey="revision"
          loading={loading}
          columns={columns}
          dataSource={items}
          pagination={false}
          rowSelection={{
            selectedRowKeys: selected,
            onChange: handleSelect,
            hideSelectAll: true,
          }}
          data-testid="history-revisions-table"
        />
      </Card>

      {(diff !== null || diffLoading) && (
        <Card
          title={diff?.label ?? t("history_tab.diff_loading")}
          loading={diffLoading}
          style={{ marginTop: 16 }}
        >
          {/* Monaco does not forward data-testid — the wrapper carries it. */}
          <div data-testid="history-diff-editor">
            <DiffEditor
              height="420px"
              language="yaml"
              theme="vs-dark"
              original={diff?.older ?? ""}
              modified={diff?.newer ?? ""}
              options={{ readOnly: true, renderSideBySide: true, minimap: { enabled: false } }}
            />
          </div>
        </Card>
      )}
    </div>
  );
}
