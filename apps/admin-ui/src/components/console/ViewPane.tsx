/**
 * ViewPane — 中栏三视图(对话 / 轨迹 / 工作区)共用的容器,从
 * ``pages/agent_detail/PlaygroundTab.tsx`` 原样搬出(PR-B Task 2),好让
 * ``ConversationDetail`` 等其它调试台外壳复用同一份「首次激活才挂载、之后
 * 只切显隐」逻辑。
 */
import { useRef } from "react";

/** 中栏三视图。会话级内存态:切会话 / 换 agent 回「对话」(spec §九「壳」)。 */
export type ConsoleView = "chat" | "trajectory" | "workspace";

/** 视图体:占满头部与输入区之间的剩余高度。`Transcript` / `TrajectoryView`
 *  自己就是 `flex:1` 且自带内滚,所以这层只开最小尺寸;工作区没有内滚容器,
 *  单独多一个 `overflow:auto`(见 `VIEW_PANE_SCROLL_STYLE`)。 */
const VIEW_PANE_STYLE: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  flex: 1,
  minHeight: 0,
};
const VIEW_PANE_SCROLL_STYLE: React.CSSProperties = {
  ...VIEW_PANE_STYLE,
  overflow: "auto",
};
/** 非当前视图:`hidden` 属性只是 UA 的 `display:none`,**任何**行内 display
 *  都能盖过它 —— 上面两个常量恰好都带 `display:flex`,所以隐藏态必须显式写
 *  行内 `display:none`,不能只挂属性。 */
const VIEW_PANE_HIDDEN_STYLE: React.CSSProperties = { display: "none" };

/** 视图容器:**首次激活才挂载,之后常驻**(只切显隐)。
 *
 *  - 为什么激活过就不卸载:卸载会丢掉 React 状态 —— 轨迹的选中记录 / 时间轴
 *    选区 / 搜索词 / 折叠集 / 缩放视口,还会让常驻的 `focusRequest` 在重挂时
 *    重新受理一遍,把读者弹回当初那条记录。(**注意**:`display:none` 保不住
 *    滚动偏移 —— 浏览器在元素不可见时会把 `scrollTop` 归零,所以「回到原处
 *    接着看」这件事这里并不成立,能保住的是上面那些 React 状态。)
 *  - 为什么不一上来就全挂:`WorkspacePanel` 一挂就打 `GET /workspace`,
 *    `TrajectoryView` 一挂就把加载窗口内的 pending 历史 run 全部回放(最多
 *    20 轮)—— 读者可能整场都待在对话视图,这些都是白发的请求(修复轮 2)。 */
export function ViewPane({
  view,
  active,
  scroll = false,
  children,
}: {
  view: ConsoleView;
  active: boolean;
  scroll?: boolean;
  children: React.ReactNode;
}): React.ReactElement {
  // 单调闩:`active` 翻 true 本身就是一次重渲染,所以这里读到的一定是最新值,
  // 不需要额外的 state / effect(重复置 true 幂等,StrictMode 双跑也无副作用)。
  const seen = useRef(active);
  if (active) seen.current = true;

  return (
    <div
      data-testid={`console-view-pane-${view}`}
      data-active={active ? "true" : undefined}
      hidden={!active}
      aria-hidden={active ? undefined : "true"}
      style={
        active ? (scroll ? VIEW_PANE_SCROLL_STYLE : VIEW_PANE_STYLE) : VIEW_PANE_HIDDEN_STYLE
      }
    >
      {seen.current ? children : null}
    </div>
  );
}
