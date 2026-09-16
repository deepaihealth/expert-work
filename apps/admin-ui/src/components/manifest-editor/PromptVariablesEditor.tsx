/**
 * Dynamic-Prompt — the Jinja toggle + declared-variable editor for the system
 * prompt. When Jinja is on, the ``template`` is rendered per-run with the run
 * request's ``inputs``; the declared variables are the contract those inputs
 * are validated against. ``trusted`` decides whether a value renders verbatim
 * or is spotlight-fenced as DATA (default trusted — an owner-set posture).
 * Every control emits the FULL merged manifest via the form_model writers.
 *
 * B-61 — three ways out of this editor can orphan an MCP ``arg_bindings``
 * entry, i.e. leave it naming something ``system_prompt.variables`` no longer
 * declares. ``_check_arg_bindings`` rule (4) REJECTS such a manifest outright,
 * so the operator gets a 422 nothing on screen explains, while the MCP tab
 * happily renders the orphaned name as if it were a real choice:
 *
 *   removeVar  — delete the variable          → refuse, and say who uses it
 *   patchVar   — RENAME it (feels harmless)   → refuse the name change
 *   jinja off  — drops the whole block at once → confirm, then clear with it
 *
 * Each guard sits on the WRITE PATH, never on the control. ``disabled`` is an
 * affordance — it tells a person not to bother — and it only suppresses events
 * the browser itself dispatches; a programmatic value set still reaches React's
 * onChange. When an affordance fails the worst outcome should be an ugly UI,
 * never an invalid manifest, so the invariant is held one layer down: by these
 * handlers, and for the Jinja switch by ``setPromptJinja`` itself.
 *
 * This is prevention, not validation: the backend stays the authority (YAML,
 * ``PUT …/draft`` and template copies all bypass this editor). The job here is
 * only to stop the operator walking into it blind — same reasoning as the MCP
 * picker's drop-confirm, and the same shape of message (a count plus the list).
 */
import { useRef, type CSSProperties, type ReactNode } from "react";
import { App, Button, Input, Switch, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { FieldHelp } from "../FieldHelp";
import {
  bindingsUsingVariable,
  type BindingUse,
  readPromptJinja,
  readPromptVariables,
  setPromptJinja,
  setPromptVariables,
  type PromptVariableFields,
} from "./form_model";

const { Text } = Typography;

const SECTION: CSSProperties = { marginBottom: 24 };

function Heading({ children }: { children: ReactNode }) {
  return <h3 style={{ fontSize: 15, margin: "0 0 12px" }}>{children}</h3>;
}

interface PromptVariablesEditorProps {
  formData: unknown;
  onChange: (data: unknown) => void;
}

export function PromptVariablesEditor({
  formData,
  onChange,
}: PromptVariablesEditorProps) {
  const { t } = useTranslation();
  // 拒删的说明走 App.useApp() 的 modal(项目惯例 —— 静态 Modal 在测试里不渲染)。
  const { modal } = App.useApp();
  const scrollRef = useRef<HTMLDivElement>(null);
  const jinja = readPromptJinja(formData);
  const variables = readPromptVariables(formData);

  /** 绑着这一行变量的工具参数。空 = 这行随便删随便改。 */
  const boundUses = (row: PromptVariableFields) =>
    bindingsUsingVariable(formData, row.name ?? "");

  /** 列出「哪个服务器的哪个工具的哪个参数」—— 两台服务器上的同名工具不能渲染
   *  成一模一样的两行。 */
  const renderUses = (uses: BindingUse[]) => (
    <ul style={{ margin: "0 0 8px", paddingLeft: 18 }}>
      {uses.map((use) => (
        <li key={`${use.server}/${use.tool}/${use.param}`}>
          {t("agent_form.prompt_var_remove_blocked_item", {
            server: use.server,
            tool: use.tool,
            param: use.param,
          })}
        </li>
      ))}
    </ul>
  );

  const patchVar = (i: number, patch: Partial<PromptVariableFields>): void => {
    // 改名要在**写入口**拦,不能只靠输入框的 disabled:disabled 只压住浏览器
    // 自己派发的交互事件,程序化赋值(扩展、密码管理器、devtools:native setter
    // + dispatchEvent("input"))照样走到 React 的 onChange。trusted / required /
    // 说明这些字段动了不会造成孤儿,照常放行 —— 只挡 name。
    // 用 ``"name" in patch`` 而不是 ``patch.name !== undefined``:展开一个带
    // ``name: undefined`` 的对象会把这个键拷进来,而 exactOptionalPropertyTypes
    // 是关的,所以那种 patch 类型上合法、语义上是「把名字清空」。今天四个调用点
    // 都构不出它,但闸的判据不该依赖调用点的自觉。
    if ("name" in patch && boundUses(variables[i]).length > 0) return;
    const next = variables.map((row, idx) =>
      idx === i ? { ...row, ...patch } : row,
    );
    onChange(setPromptVariables(formData, next));
  };

  /**
   * 关掉 Jinja = 把整个 variables 块删掉,于是**每一条**绑定都成孤儿。
   *
   * 这里是确认不是拒绝:关掉动态提示词是用户的真实意图(和「取消勾最后一个 MCP
   * 服务器 = 关掉 MCP」完全同构),连带清掉绑定是符合预期的语义。要挡的不是这个
   * 决定,而是「看不见的东西被无声删掉」—— 所以照 T8 那条追加要求的口径:报出
   * 会同时删掉几条、逐条列出来。
   *
   * 清理本身在 ``setPromptJinja`` 里做,不在这个回调里:走到这一步的不只这一个
   * 开关,而不变式不该由某个调用点的自觉来守。
   */
  const toggleJinja = (on: boolean): void => {
    const uses = variables.flatMap(boundUses);
    if (on || uses.length === 0) {
      onChange(setPromptJinja(formData, on));
      return;
    }
    modal.confirm({
      title: t("agent_form.prompt_jinja_off_title", { count: uses.length }),
      okText: t("agent_form.prompt_jinja_off_ok"),
      cancelText: t("agent_form.prompt_jinja_off_cancel"),
      content: (
        <div>
          {renderUses(uses)}
          <Text type="secondary" style={{ fontSize: 12 }}>
            {t("agent_form.prompt_jinja_off_hint")}
          </Text>
        </div>
      ),
      onOk: () => onChange(setPromptJinja(formData, false)),
    });
  };
  const addVar = (): void => {
    const nextIndex = variables.length;
    onChange(
      setPromptVariables(formData, [
        ...variables,
        { name: "", trusted: true, required: true, description: "" },
      ]),
    );
    // 新行落在滚动容器底部,不滚过去的话点击看起来毫无反应(终审第二轮)——
    // 滚到底 + 聚焦新行 name 输入框(顺带键盘可达)。
    requestAnimationFrame(() => {
      const el = scrollRef.current;
      if (el) el.scrollTop = el.scrollHeight;
      document
        .querySelector<HTMLInputElement>(
          `[data-testid="af-prompt-var-name-${nextIndex}"]`,
        )
        ?.focus();
    });
  };
  const removeVar = (i: number): void => {
    const uses = boundUses(variables[i]);
    if (uses.length > 0) {
      // design §5.4 —— 拒删,并列出哪几个工具在用(跟删技能 / 删子 Agent 同一
      // 口径)。不是确认框:这里没有「继续」那一档,因为继续下去就是一份存不进
      // 去的 manifest。
      modal.info({
        title: t("agent_form.prompt_var_remove_blocked", {
          count: uses.length,
        }),
        okText: t("agent_form.prompt_var_remove_blocked_ok"),
        content: (
          <div>
            {renderUses(uses)}
            <Text type="secondary" style={{ fontSize: 12 }}>
              {t("agent_form.prompt_var_remove_blocked_hint")}
            </Text>
          </div>
        ),
      });
      return;
    }
    onChange(
      setPromptVariables(
        formData,
        variables.filter((_, idx) => idx !== i),
      ),
    );
  };

  return (
    <section data-testid="af-prompt-vars" style={SECTION}>
      <Heading>
        {t("agent_form.section_prompt_vars")}
        <FieldHelp
          text={t("agent_form.section_prompt_vars_help")}
          testId="af-prompt-vars"
        />
      </Heading>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginBottom: 8,
        }}
      >
        <Switch
          checked={jinja}
          data-testid="af-prompt-jinja"
          aria-label={t("agent_form.prompt_jinja_label")}
          onChange={toggleJinja}
        />
        <Text>{t("agent_form.prompt_jinja_label")}</Text>
      </div>
      <Text type="secondary" style={{ display: "block", marginBottom: 12 }}>
        {t("agent_form.prompt_jinja_hint")}
      </Text>

      {jinja && (
        <>
          {variables.length > 0 && (
            <Text
              type="secondary"
              data-testid="af-prompt-vars-count"
              style={{ display: "block", marginBottom: 6, fontSize: 12 }}
            >
              {t("agent_form.prompt_vars_count", { count: variables.length })}
            </Text>
          )}
          {/* BUG-4:变量一多把整页撑成一面墙 —— 列表内滚,添加按钮留在
              容器外恒可见。 */}
          <div
            ref={scrollRef}
            data-testid="af-prompt-vars-scroll"
            style={{ maxHeight: "40vh", overflowY: "auto", marginBottom: 8 }}
          >
          {variables.map((row, i) => {
            const uses = boundUses(row);
            return (
            <div
              key={i}
              data-testid={`af-prompt-var-row-${i}`}
              style={{
                display: "flex",
                // 说明那行小字换到下面独占一行(它 flexBasis:100%)——
                // 140 字符的一句话本来就不该和五个控件抢同一行。见下。
                flexWrap: "wrap",
                gap: 8,
                marginBottom: 8,
                alignItems: "center",
              }}
            >
              {/* 真正拦住改名的是 patchVar;这里的 disabled 只是告诉人别白费劲。 */}
              <Input
                style={{ width: 160 }}
                value={row.name ?? ""}
                disabled={uses.length > 0}
                data-testid={`af-prompt-var-name-${i}`}
                aria-label={t("agent_form.prompt_var_name")}
                placeholder={t("agent_form.prompt_var_name")}
                onChange={(e) => patchVar(i, { name: e.target.value })}
              />
              <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
                <Switch
                  size="small"
                  checked={row.trusted !== false}
                  data-testid={`af-prompt-var-trusted-${i}`}
                  aria-label={t("agent_form.prompt_var_trusted")}
                  onChange={(on) => patchVar(i, { trusted: on })}
                />
                <Text type="secondary">
                  {t("agent_form.prompt_var_trusted")}
                </Text>
              </span>
              <span style={{ display: "flex", alignItems: "center", gap: 4 }}>
                <Switch
                  size="small"
                  checked={row.required !== false}
                  data-testid={`af-prompt-var-required-${i}`}
                  aria-label={t("agent_form.prompt_var_required")}
                  onChange={(on) => patchVar(i, { required: on })}
                />
                <Text type="secondary">
                  {t("agent_form.prompt_var_required")}
                </Text>
              </span>
              <Input
                style={{ flex: 1 }}
                value={row.description ?? ""}
                data-testid={`af-prompt-var-desc-${i}`}
                aria-label={t("agent_form.prompt_var_description")}
                placeholder={t("agent_form.prompt_var_description")}
                onChange={(e) => patchVar(i, { description: e.target.value })}
              />
              <Button
                type="text"
                danger
                size="small"
                data-testid={`af-prompt-var-remove-${i}`}
                aria-label={t("agent_form.prompt_var_remove")}
                onClick={() => removeVar(i)}
              >
                {t("agent_form.prompt_var_remove")}
              </Button>
              {uses.length > 0 && (
                // 「为什么锁着」和「怎么解锁」都写在这行常驻小字里,不放 Tooltip:
                // 这个 Input 渲染出来是裸 <input disabled>,而 Chromium 不给
                // disabled 表单控件派 mouseenter、antd v5 也没有 v4 那个
                // disabled 子元素兼容层 —— 挂上去的提示一辈子不出现。
                //
                // flexBasis:100% 让它换到控件下面**独占一行**。上一版把这句
                // ~140 字符的说明塞进同一行、又去掉了 nowrap,结果把它本想讲清楚
                // 的那一行挤垮了:变量名只剩 "project_c"、Trusted/Required 塌成
                // 一列一个字母。e2e (e) 量着这一行的几何,别再把它挪回行内。
                <Text
                  type="secondary"
                  data-testid={`af-prompt-var-bound-${i}`}
                  style={{ fontSize: 12, flexBasis: "100%" }}
                >
                  {t("agent_form.prompt_var_bound_note", { count: uses.length })}
                </Text>
              )}
            </div>
            );
          })}
          </div>
          <Button
            type="dashed"
            size="small"
            data-testid="af-prompt-var-add"
            onClick={addVar}
          >
            {t("agent_form.prompt_var_add")}
          </Button>
        </>
      )}
    </section>
  );
}
