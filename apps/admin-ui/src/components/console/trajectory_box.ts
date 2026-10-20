/**
 * trajectory_box —— 调试台外壳之外的页面(对话详情、运行详情)放轨迹视图用的
 * 定高外框(B-202 ② / B-203 ⑦)。
 *
 * 账本(`.ew-ledger`)和详情面板都按「父级高度有界」写,只在框内滚动
 * (`trajectory_view.css`)。调试台的 `.ew-console` 给了这个框;别的页面直接把
 * `TrajectoryView` 放进 Card / div,账本有多长整页就多高,点账本下面的行时详情
 * 面板停在页面顶上、在视口之外。框高 = 视口高减 64px(Shell 的 Content 上下各
 * 24px 留白 + Card 内边距的余量),不高于视口:滚到框露全时,账本和面板同时都在
 * 视口里。
 */
import type { CSSProperties } from "react";

export const TRAJECTORY_BOX_STYLE: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  height: "calc(100dvh - 64px)",
  minHeight: 480,
};
