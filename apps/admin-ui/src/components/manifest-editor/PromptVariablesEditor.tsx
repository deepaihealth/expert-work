/**
 * Dynamic-Prompt — the Jinja toggle + declared-variable editor for the system
 * prompt. When Jinja is on, the ``template`` is rendered per-run with the run
 * request's ``inputs``; the declared variables are the contract those inputs
 * are validated against. ``trusted`` decides whether a value renders verbatim
 * or is spotlight-fenced as DATA (default trusted — an owner-set posture).
 * Every control emits the FULL merged manifest via the form_model writers.
 *
 * B-61 — a variable an MCP tool parameter is BOUND to may be neither removed
 * nor renamed here. Either one leaves the binding pointing at a name
 * ``system_prompt.variables`` no longer declares, which the protocol layer
 * REJECTS (``_check_arg_bindings`` rule 4) — so the operator gets a 422 they
 * cannot explain from anything on screen, while the MCP tab happily renders the
 * orphaned name as if it were a real choice. Renaming is the worse of the two
 * because it does not feel destructive at all.
 *
 * This is prevention, not validation: the backend stays the authority (YAML,
 * ``PUT …/draft`` and template copies all bypass this editor). The job here is
 * only to stop the operator walking into it blind — same reasoning as the MCP
 * picker's drop-confirm, and the same shape of message (a count plus the list).
 */
import { useRef, type CSSProperties, type ReactNode } from "react";
import { App, Button, Input, Switch, Tooltip, Typography } from "antd";
import { useTranslation } from "react-i18next";

import { FieldHelp } from "../FieldHelp";
import {
  bindingsUsingVariable,
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

  const patchVar = (i: number, patch: Partial<PromptVariableFields>): void => {
    const next = variables.map((row, idx) =>
      idx === i ? { ...row, ...patch } : row,
    );
    onChange(setPromptVariables(formData, next));
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
            <ul style={{ margin: "0 0 8px", paddingLeft: 18 }}>
              {uses.map((use) => (
                <li key={`${use.server}/${use.tool}/${use.param}`}>
                  {t("agent_form.prompt_var_remove_blocked_item", {
                    tool: use.tool,
                    param: use.param,
                  })}
                </li>
              ))}
            </ul>
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
          onChange={(on) => onChange(setPromptJinja(formData, on))}
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
                gap: 8,
                marginBottom: 8,
                alignItems: "center",
              }}
            >
              {/* 改名会把绑定指到一个不存在的名字上,而删除至少还有个按钮可以
                  当场答复 —— 输入框没法「答复」一次击键,只能不让它改。锁上的
                  理由就在旁边那行小字里,不是一个没有解释的灰框。 */}
              <Tooltip title={uses.length > 0 ? t("agent_form.prompt_var_bound_locked") : ""}>
                <Input
                  style={{ width: 160 }}
                  value={row.name ?? ""}
                  disabled={uses.length > 0}
                  data-testid={`af-prompt-var-name-${i}`}
                  aria-label={t("agent_form.prompt_var_name")}
                  placeholder={t("agent_form.prompt_var_name")}
                  onChange={(e) => patchVar(i, { name: e.target.value })}
                />
              </Tooltip>
              {uses.length > 0 && (
                <Text
                  type="secondary"
                  data-testid={`af-prompt-var-bound-${i}`}
                  style={{ fontSize: 12, whiteSpace: "nowrap" }}
                >
                  {t("agent_form.prompt_var_bound_note", { count: uses.length })}
                </Text>
              )}
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
