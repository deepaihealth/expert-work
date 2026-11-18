/**
 * B-316 — a user id rendered as the member's name, with the raw id in a
 * hover tooltip for troubleshooting. An id with no known member (service
 * account, system actor, a member list we could not read) renders as-is.
 */
import type { ReactNode } from "react";
import { Tooltip } from "antd";
import type { TFunction } from "i18next";

import type { MemberNameOf } from "../hooks/useMemberNames";

interface MemberNameProps {
  id: string;
  nameOf: MemberNameOf;
  /** How an unresolved id is shown (e.g. truncated); defaults to the id. */
  fallback?: ReactNode;
  "data-testid"?: string;
}

export function MemberName({ id, nameOf, fallback, "data-testid": testId }: MemberNameProps) {
  const name = nameOf(id);
  if (name === id) {
    return <span data-testid={testId}>{fallback ?? id}</span>;
  }
  return (
    <Tooltip title={id}>
      <span data-testid={testId}>{name}</span>
    </Tooltip>
  );
}

/** Plain-text form for places that need the id visible: "name (id)" for a
 *  member, the bare id otherwise. */
export function memberNameWithId(t: TFunction, nameOf: MemberNameOf, id: string): string {
  const name = nameOf(id);
  return name === id ? id : t("common.name_with_id", { name, id });
}
