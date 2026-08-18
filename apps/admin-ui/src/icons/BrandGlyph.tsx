import type { SVGProps } from "react";

/** Expert-Work 品牌标:圆角底板 + 「EW」连笔(E 的顶横直接接成 W 的第一笔)。
 *  底板色跟品牌主色 token 走(浅/深主题各自的 500),字白;与对外文档站 logo 同源。 */
export function BrandGlyph({ size = 20, ...rest }: SVGProps<SVGSVGElement> & { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 48 48" fill="none" aria-hidden="true" {...rest}>
      <rect x="1" y="1" width="46" height="46" rx="11" fill="var(--ew-color-brand-500)" />
      <g transform="translate(6 7) scale(.75)">
        <path
          d="M7 36V12h17l4 24 5-14 5 14 4-24M7 24h10M7 36h10"
          fill="none"
          stroke="#fff"
          strokeWidth="4.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </g>
    </svg>
  );
}
