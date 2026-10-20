/**
 * EventCard — the 「原始」 tab's per-frame card. B-202: the time shown is the
 * server's (the ``{server_ms}-{seq}`` frame id), not the browser's receive
 * time — a replayed run's frames are all received at the moment of replay.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import i18n from "../../i18n";

import type { SseEvent } from "../../api/sessions";
import { EventCard } from "../EventCard";

// 会话 0e878fd6 实例:事件发生在服务器时间 1792481680066,回放发生在 21 分钟后。
const SERVER_MS = 1792481680066;
const REPLAYED_AT = new Date(SERVER_MS + 21 * 60_000).toISOString();

function frame(over: Partial<SseEvent>): SseEvent {
  return { id: null, event: "updates", data: {}, rawData: "", receivedAt: REPLAYED_AT, ...over };
}

describe("EventCard time", () => {
  let priorLang: string;
  beforeEach(async () => {
    priorLang = i18n.language;
    await i18n.changeLanguage("zh-CN");
  });
  afterEach(async () => {
    await i18n.changeLanguage(priorLang);
  });

  it("shows the server time from the frame id, not the (replay) receive time", () => {
    render(<EventCard evt={frame({ id: `${SERVER_MS}-45` })} />);
    const time = screen.getByTestId("event-card-time");
    expect(time).toHaveTextContent(new Date(SERVER_MS).toLocaleTimeString());
    expect(time).not.toHaveTextContent(new Date(REPLAYED_AT).toLocaleTimeString());
    // id 本身照旧显示在旁边。
    expect(screen.getByText(`${SERVER_MS}-45`)).toBeInTheDocument();
  });

  it("a frame without a server id falls back to the receive time, labelled as such", () => {
    render(<EventCard evt={frame({ id: null, event: "end" })} />);
    expect(screen.getByTestId("event-card-time")).toHaveTextContent(
      `收到 ${new Date(REPLAYED_AT).toLocaleTimeString()}`,
    );
  });
});
