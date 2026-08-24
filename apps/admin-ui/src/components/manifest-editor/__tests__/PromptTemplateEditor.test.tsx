import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * The Monaco stub drives ``beforeMount`` with a fake ``monaco`` so the Jinja
 * language + completion provider registration code runs under jsdom. The
 * registered completion provider is captured module-side for assertion.
 */
let capturedProvider:
  | {
      provideCompletionItems: (
        model: unknown,
        position: unknown,
      ) => { suggestions: Array<{ label: string; detail?: string }> };
    }
  | undefined;

const fakeMonaco = {
  languages: {
    register: vi.fn(),
    setMonarchTokensProvider: vi.fn(),
    registerCompletionItemProvider: vi.fn(
      (_lang: string, provider: unknown) => {
        capturedProvider = provider as typeof capturedProvider;
      },
    ),
    CompletionItemKind: { Variable: 4 },
  },
  editor: { defineTheme: vi.fn() },
};

vi.mock("@monaco-editor/react", () => {
  const Editor = ({
    value,
    onChange,
    beforeMount,
  }: {
    value?: string;
    onChange?: (v: string | undefined) => void;
    beforeMount?: (monaco: unknown) => void;
  }) => {
    beforeMount?.(fakeMonaco);
    return (
      <textarea
        data-testid="monaco-stub"
        value={value}
        onChange={(e) => onChange?.(e.target.value)}
      />
    );
  };
  return { default: Editor };
});

import { PromptTemplateEditor } from "../widgets/PromptTemplateEditor";

describe("PromptTemplateEditor", () => {
  it("renders the value and reports edits", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <PromptTemplateEditor
        value="Hello {{ name }}"
        variables={[]}
        onChange={onChange}
      />,
    );

    expect(screen.getByTestId("af-prompt-monaco")).toBeInTheDocument();
    const ta = screen.getByTestId("monaco-stub") as HTMLTextAreaElement;
    expect(ta.value).toBe("Hello {{ name }}");
    await user.type(ta, "!");
    expect(onChange).toHaveBeenCalled();
  });

  it("registers the Jinja language and a completion provider", () => {
    render(<PromptTemplateEditor value="" variables={[]} onChange={vi.fn()} />);
    expect(fakeMonaco.languages.setMonarchTokensProvider).toHaveBeenCalled();
    expect(fakeMonaco.editor.defineTheme).toHaveBeenCalled();
    expect(capturedProvider).toBeDefined();
  });

  it("suggests declared variables, skipping unnamed rows", () => {
    render(
      <PromptTemplateEditor
        value=""
        variables={[
          {
            name: "user_name",
            trusted: true,
            required: true,
            description: "the user",
          },
          { name: "", trusted: true, required: true, description: "" },
        ]}
        onChange={vi.fn()}
      />,
    );
    const model = {
      getWordUntilPosition: () => ({ startColumn: 1, endColumn: 1 }),
    };
    const result = capturedProvider?.provideCompletionItems(model, {
      lineNumber: 1,
    });
    const labels = result?.suggestions.map((s) => s.label) ?? [];
    expect(labels).toEqual(["user_name"]);
    expect(result?.suggestions[0]?.detail).toBe("the user");
  });
});

describe("PromptTemplateEditor fullscreen (BUG-4)", () => {
  it("opens the expand modal with a second live editor bound to the same value", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <PromptTemplateEditor
        value="Hello {{ name }}"
        variables={[]}
        onChange={onChange}
      />,
    );

    expect(screen.getAllByTestId("monaco-stub")).toHaveLength(1);
    await user.click(screen.getByTestId("af-prompt-expand"));
    const stubs = screen.getAllByTestId("monaco-stub") as HTMLTextAreaElement[];
    expect(stubs).toHaveLength(2);
    expect(stubs[1].value).toBe("Hello {{ name }}");
    await user.type(stubs[1], "!");
    expect(onChange).toHaveBeenCalled();
  });
});
