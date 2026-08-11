/**
 * In-app handbook doc loader (spec 2026-08-11 §4).
 *
 * Markdown files under ``./tenant/*.md`` (usage docs, visible to everyone)
 * and ``./ops/*.md`` (platform-ops docs, ``system_admin`` only — gated by
 * the page, see ``pages/Handbook.tsx``) are bundled into the app via Vite's
 * ``?raw`` glob import — no runtime fetch, no CMS. Each file's front-matter
 * supplies ``title``/``order`` (required by contract; Task 4/5 only add new
 * ``.md`` files here, nothing else changes).
 */

export interface DocEntry {
  /** File name without the ``.md`` extension — used as the route param
   *  (``/handbook/:slug``) and the antd Menu item key. */
  slug: string;
  title: string;
  order: number;
  body: string;
}

interface FrontMatter {
  title: string;
  order: number;
  group?: string;
  body: string;
}

/**
 * Hand-rolled front-matter parser (no ``gray-matter`` — it depends on
 * Node's ``Buffer``, which doesn't exist in the browser bundle).
 *
 * Recognizes a leading ``---``/``---``-delimited block of ``key: value``
 * lines and only reads ``title``/``order``/``group`` — anything else in the
 * block is ignored. A file with no leading ``---`` line (or an unterminated
 * block) has no front-matter: the whole file is the body, and ``title``
 * falls back to the caller-supplied default (the slug) with ``order: 0``.
 */
export function parseFrontMatter(raw: string, slugFallback: string): FrontMatter {
  const lines = raw.split("\n");
  const fallback: FrontMatter = { title: slugFallback, order: 0, body: raw };
  if (lines[0]?.trim() !== "---") return fallback;

  const endIdx = lines.indexOf("---", 1);
  if (endIdx === -1) return fallback;

  const fields: Record<string, string> = {};
  for (const line of lines.slice(1, endIdx)) {
    const sep = line.indexOf(":");
    if (sep === -1) continue;
    const key = line.slice(0, sep).trim();
    const value = line.slice(sep + 1).trim();
    if (key === "title" || key === "order" || key === "group") {
      fields[key] = value;
    }
  }

  const body = lines
    .slice(endIdx + 1)
    .join("\n")
    .replace(/^\n+/, "");
  const order = fields.order !== undefined ? Number(fields.order) : 0;
  return {
    title: fields.title || slugFallback,
    order: Number.isFinite(order) ? order : 0,
    group: fields.group,
    body,
  };
}

function slugFromPath(path: string): string {
  const base = path.split("/").pop() ?? path;
  return base.replace(/\.md$/, "");
}

function buildEntries(modules: Record<string, string>): DocEntry[] {
  return Object.entries(modules)
    .map(([path, raw]) => {
      const slug = slugFromPath(path);
      const { title, order, body } = parseFrontMatter(raw, slug);
      return { slug, title, order, body };
    })
    .sort((a, b) => a.order - b.order || a.slug.localeCompare(b.slug));
}

const tenantModules = import.meta.glob("./tenant/*.md", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const opsModules = import.meta.glob("./ops/*.md", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** Every handbook doc, grouped and sorted by front-matter ``order``. */
export function loadDocs(): { tenant: DocEntry[]; ops: DocEntry[] } {
  return {
    tenant: buildEntries(tenantModules),
    ops: buildEntries(opsModules),
  };
}
