/**
 * Shared client-side pagination policy — the BUG-3 口径 (20/page, no size
 * changer, pager hidden when everything fits) that SkillsList, SkillPicker
 * and SettingsApiKeys all follow. One source so a policy change can't
 * silently diverge per page.
 */
export const TABLE_PAGE_SIZE = 20;

/** Spread into an antd ``Table``/``Pagination`` ``pagination`` prop; add
 *  ``current``/``onChange`` at the call site when the page is controlled. */
export const TABLE_PAGINATION = {
  pageSize: TABLE_PAGE_SIZE,
  showSizeChanger: false,
  hideOnSinglePage: true,
} as const;
