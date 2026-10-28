/**
 * Typed JSON contract fixtures — B-175 P2-PR4.
 *
 * A JSON import is typed as written, with every string literal widened to
 * ``string``, so ``fixture as unknown as T`` would accept anything. Passing
 * it through :func:`typedFixture` checks it against ``T`` (string-literal
 * unions widened the way a JSON import widens them): a fixture that drops a
 * required key or changes a value's type fails ``pnpm typecheck``.
 */

/** ``T`` as a JSON import would type it: string-literal unions become ``string``. */
export type AsJson<T> = T extends string
  ? string
  : T extends readonly (infer U)[]
    ? AsJson<U>[]
    : T extends object
      ? { [K in keyof T]: AsJson<T[K]> }
      : T;

/** ``json`` as ``T``, checked against ``T``'s shape at compile time. */
export function typedFixture<T>(json: AsJson<T>): T {
  return json as T;
}
