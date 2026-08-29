/** 「这次运行用的是哪一版配置」—— Run 详情元数据里的一行。
 *
 *  为什么需要它:Agent 配置是**原地编辑**的,`agent_version` 编辑前后完全
 *  一样,所以版本号回答不了这个问题。后端在构建 agent 那一刻把 manifest 的
 *  内容哈希写进 `agent_run.agent_spec_sha256`;这里把哈希翻译成人能用的东西
 *  —— 版本号,以及「它是不是现在还生效的那一版」。
 *
 *  两次读:Agent 详情给出当前生效的哈希,修订历史给出哈希 → 版本号的映射。
 *  任一失败都退回显示短哈希本身:一个认不出来源的哈希仍然比什么都没有强,
 *  两条 run 的哈希能不能对上是肉眼可判的。
 */
import { Skeleton, Tag, Tooltip } from "antd";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { getAgent, listRevisions } from "../../api/agents";
import type { TenantScope } from "../../api/client";

interface SpecVersionFieldProps {
  /** `agent_run.agent_spec_sha256` —— null/空 = 没记录(见 unrecorded 文案)。 */
  sha?: string | null;
  agentName: string;
  agentVersion: string;
  tenantScope?: TenantScope;
}

interface Resolved {
  /** 修订号;null = 这个哈希不在修订历史里(通常是创建后从没编辑过的那一版)。 */
  revision: number | null;
  isCurrent: boolean;
}

export function SpecVersionField({
  sha,
  agentName,
  agentVersion,
  tenantScope,
}: SpecVersionFieldProps) {
  const { t } = useTranslation();
  const [resolved, setResolved] = useState<Resolved | null>(null);
  const [resolving, setResolving] = useState(false);

  useEffect(() => {
    if (!sha || !agentName || !agentVersion) {
      setResolved(null);
      return;
    }
    let cancelled = false;
    setResolving(true);
    Promise.all([
      getAgent(agentName, agentVersion, tenantScope),
      listRevisions(agentName, agentVersion, tenantScope),
    ])
      .then(([detail, history]) => {
        if (cancelled) return;
        const hit = history.items.find((r) => r.spec_sha256 === sha);
        setResolved({
          revision: hit?.revision ?? null,
          isCurrent: detail.record.spec_sha256 === sha,
        });
      })
      .catch(() => {
        // 退回短哈希 —— 见文件头。
        if (!cancelled) setResolved(null);
      })
      .finally(() => {
        if (!cancelled) setResolving(false);
      });
    return () => {
      cancelled = true;
    };
  }, [sha, agentName, agentVersion, tenantScope]);

  if (!sha) {
    return (
      <Tooltip title={t("run_detail.spec_version_unrecorded_hint")}>
        <span style={{ color: "var(--ew-text-tertiary)" }}>
          {t("run_detail.spec_version_unrecorded")}
        </span>
      </Tooltip>
    );
  }

  const short = sha.slice(0, 12);

  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}>
      <Tooltip title={t("run_detail.spec_version_hint")}>
        <span className="mono">
          {resolved?.revision != null
            ? t("run_detail.spec_version_revision", { n: resolved.revision })
            : short}
        </span>
      </Tooltip>
      {resolving && <Skeleton.Button active size="small" style={{ width: 64, height: 20 }} />}
      {!resolving && resolved != null && (
        <Tooltip title={resolved.isCurrent ? undefined : t("run_detail.spec_version_changed_hint")}>
          <Tag color={resolved.isCurrent ? "default" : "orange"} style={{ marginInlineEnd: 0 }}>
            {resolved.isCurrent
              ? t("run_detail.spec_version_current")
              : t("run_detail.spec_version_changed")}
          </Tag>
        </Tooltip>
      )}
    </span>
  );
}
