/**
 * ``useRateBook`` — the rate cards a console session needs to price its
 * usage (B-42): the agent's own model plus every ``(provider, model)`` its
 * workers reported, each fetched once and kept for the life of the page.
 *
 * Before B-42 ``PlaygroundTab`` fetched exactly one card (the agent model's)
 * and priced every token with it. Cost is system_admin-only (rate cards are;
 * cross-tenant W4 D2), so ``enabled=false`` fetches nothing and returns
 * ``null`` — the cost surfaces hide on their existing "no data" path.
 */
import { useEffect, useMemo, useRef, useState } from "react";

import { rateKey, type RateBook } from "../../../api/cost";
import { listRateCards, type RateCardRecord } from "../../../api/rate_card";

export interface ModelRef {
  provider: string;
  model: string;
}

export function useRateBook(args: {
  enabled: boolean;
  /** The agent's ``spec.model``; ``null`` when incomplete → nothing prices. */
  agentModel: ModelRef | null;
  /** Models the session's usage attributed tokens to (``attributedModelsOf``). */
  models: readonly ModelRef[];
}): RateBook | null {
  const { enabled, agentModel, models } = args;
  // Fetched-or-in-flight keys → card (``null`` = looked up, no card). Kept in
  // a ref so a fetch fires once per key; ``version`` re-renders on arrival.
  const cardsRef = useRef(new Map<string, RateCardRecord | null>());
  const [version, setVersion] = useState(0);
  const agentKey = agentModel ? rateKey(agentModel.provider, agentModel.model) : null;
  // A stable signature of the wanted keys, so the effect runs when a *new*
  // model shows up rather than on every frame's fresh ``models`` array.
  const wanted = useMemo(() => {
    const refs = agentModel ? [agentModel, ...models] : [...models];
    const keys = new Map<string, ModelRef>();
    for (const r of refs) keys.set(rateKey(r.provider, r.model), r);
    return keys;
  }, [agentModel, models]);
  const wantedSig = [...wanted.keys()].join("|");

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    for (const [key, ref] of wanted) {
      if (cardsRef.current.has(key)) continue;
      cardsRef.current.set(key, null);
      void listRateCards({ provider: ref.provider, model: ref.model })
        .then((rows) => {
          if (cancelled) return;
          cardsRef.current.set(key, rows[0] ?? null);
          setVersion((v) => v + 1);
        })
        .catch(() => {
          // No rate / not authorized → cost simply hidden.
        });
    }
    return () => {
      cancelled = true;
    };
    // ``wanted`` is derived from ``wantedSig``; keying on the signature keeps
    // the effect from re-running on identical-content arrays.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, wantedSig]);

  return useMemo(() => {
    if (!enabled) return null;
    const byModel = new Map<string, RateCardRecord>();
    for (const [key, card] of cardsRef.current) if (card !== null) byModel.set(key, card);
    return { agentKey, byModel };
    // ``version`` bumps when a card lands; it is the actual dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, agentKey, version]);
}
