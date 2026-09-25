# B-105 输出上限 / 思考上限 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「输出上限（含思考）」对每家厂商都真的生效，新增只在真支持的模型上可用的「思考长度上限」，并且截断成不可用回答时让用户看得见。

**Architecture:** 模型目录（`ModelEntry`）登记每个模型「用哪个字段表示合计上限」「厂商上限多少」「支不支持思考上限」「档位怎么映射」；agent_factory 按目录把 manifest 翻译成请求字段，经 `OpenAIProvider` 新增的 `output_cap_payload` 合进请求体（流式、非流式同一处）。截断在 provider 层标准化成 `response_metadata`，agent 节点判定后抛 `OutputTruncatedError`；看图快看被厂商拒掉时退回正常看图。

**Tech Stack:** Python 3.12 / pydantic v2 / pytest（`uv run --no-sync pytest`）；admin-ui React + antd + vitest（`pnpm typecheck`）。

**Spec:** `docs/superpowers/specs/2026-09-24-output-thinking-caps-design.md`（§8 是实测结果，目录取值以它为准）

## Global Constraints

- 平台语义：「输出上限」= **思考 + 回答合计**。
- `max_tokens` 为空 = 请求里不带上限（厂商默认）；Anthropic 为空时发 **4096**（和今天逐字节一致，Anthropic 必须带这个字段）。
- 同一请求**只发一个**上限字段（豆包两个同时发会 400，实测）。
- `output_cap_field` 取值（实测，§8.1 / §8.7）：GLM 全部、DeepSeek 全部 = `max_tokens`；kimi 全部、通义 3.8-max / 3.7-max / 3.6-plus / 3.5-plus、豆包全部、OpenAI 全部 = `max_completion_tokens`；**qwen3-max、qwen3-vl-plus、qwen3-vl-flash = `split`**；Anthropic = `max_tokens`；目录外模型：provider 为 openai / azure 时 `max_completion_tokens`（OpenAI 推理模型拒收 `max_tokens`；Azure 部署名永远不在目录里），其余（自部署等）`max_tokens`。
- `max_output_tokens`（实测）：glm-5.3 / 5.3-flash / 5.2 / 5.1 / 4.7 / 4.6 / 5v-turbo = 131072；glm-4.6v = 32768；glm-4.5v = 16384；deepseek-v4-pro / flash = 393216；doubao-seed-2-1-pro-260628 = 262144；其余 None（不校验）。
- `thinking_cap=True`：qwen3.8-max、qwen3.7-max、qwen3.6-plus、qwen3.5-plus、qwen3-max、qwen3-vl-plus、qwen3-vl-flash；其余 False。
- 在 `thinking_cap=False` 的目录内模型上填 `thinking_max_tokens` → 构建报错（`AgentFactoryError`，保存时 dry-run 构建即拒）。
- OpenAI / Azure / Anthropic 的档位映射**不改**（拍板点 ②a）。
- 旧默认值 4096 在非 Anthropic 模型上**加载时归一成空**（见 Task 2 裁定，替代 spec §4.3 的数据迁移）。
- 配置页文案简洁；字段名「输出上限（含思考）」「思考长度上限」；不支持时文案「该模型只能调思考档位，不能限制思考长度。」
- 截断错误文案：「模型输出被截断（已用满输出上限 {cap}，含思考）。请在模型配置里调大『输出上限』，或者降低思考档位。」
- 平台文本零租户内容（注释 / 文案不出现客户名、文件名）。
- 本地测试：`uv run --no-sync pytest …`；前端 `pnpm -C apps/admin-ui typecheck` + `pnpm -C apps/admin-ui test -- <file>`（**不要**用裸 `tsc --noEmit`）。

## Rulings（写计划时定的，与 spec 的差异）

1. **spec §4.3 数据迁移 → 改成加载时归一**：`ModelSpec` 的 before-validator 把「非 Anthropic 且 `max_tokens == 4096`」丢掉。理由：库里的 4096 是旧默认值被表单写回，和「没设」分不出来；归一在加载处一次覆盖 spec_json、草稿、历史 revision、平台模板、外部 API 提交的 manifest 五个来源，还不用改 `spec_sha256`。代价：非 Anthropic 用户想精确设 4096 做不到（设 4095 / 4097 即可），写进字段说明。
2. **spec §4.1 `effort_off` 不做**：关思考的线格式仍在 `_thinking_disable_payload`，模型之间的差异用已有的 `always_thinking` 表达（glm-5.3 补标即修好 400）。只把档位映射 `effort_map` 挪进目录。
3. **spec §4.2 Anthropic 默认**：为空时仍发 4096（不引入未经实调的新默认值）。
4. **截断错误不归 `GuidedTimeoutError`**（那是工具错误）：新增 `OutputTruncatedError(RuntimeError)`，放 `orchestrator/llm/truncation.py`，在 agent 节点里、路由之后抛，天然不触发 fallback。
5. **llm 缓存键**：`middleware_assembly` 传 `model.max_tokens or 0`，不改 runtime 包的类型。

## Review Focus

1. **拼合计（split）三款开了思考但没设上限**：不能发 `thinking_budget`/`max_tokens` 里的任何一个（行为与今天一致）→ Task 2 测试 `test_split_without_cap_sends_nothing`。
2. **split 下用户填的思考上限 ≥ 输出上限**：必须构建报错，否则 `cap − T ≤ 0` 发出去厂商 400 → Task 2 测试 `test_split_thinking_cap_must_be_below_output_cap`。
3. **fallback 链里混着不同厂商**：每个节点按自己的目录条目发字段（GLM 主、通义备时不能串字段）→ Task 2 测试 `test_fallback_nodes_use_their_own_cap_field`。
4. **截断但带着工具调用**：最后一个工具调用的参数被截，`_parse_arguments` 会静默给 `{}`；必须当作不可用报错，而不是带空参数去执行工具 → Task 3 测试 `test_truncated_with_tool_calls_is_unusable`。
5. **快看被拒的是 401 / 429**：这两种不是参数错，不能退回正常看图（换个配置也一样失败，还多耗一次）→ Task 3 测试 `test_quick_fallback_skips_auth_and_rate_limit`。

---

### Task 1: 目录新字段 + 档位映射进目录 + 思考长度上限

**Files:**
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/model_catalog.py`（`ModelEntry` 加字段、模块 docstring 加上架规矩、各条目取值与修正）
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py:~185`（`ModelSpec` 加 `thinking_max_tokens`）
- Modify: `services/orchestrator/src/orchestrator/agent_factory.py:2077-2230`（`_MAX_HIGH_LOW_EFFORT` 删除改读目录；`_thinking_budget`；`_thinking_enable_payload` 豆包分支；`_build_provider` 加 thinking_cap 闸门，anthropic 与 compat 两处）
- Test: `packages/expert-work-protocol/tests/test_model_catalog.py`、`services/orchestrator/tests/test_agent_factory.py`

**Interfaces:**
- Produces: `ModelEntry.output_cap_field: Literal["max_tokens", "max_completion_tokens", "split"] = "max_tokens"`、`ModelEntry.max_output_tokens: int | None = None`、`ModelEntry.thinking_cap: bool = False`、`ModelEntry.effort_map: dict[str, str] | None = None`（键为平台档位 `low/medium/high/max`，缺省键按原值发）、`ModelSpec.thinking_max_tokens: int | None`（`gt=0`）。Task 2 / Task 4 读这些字段。

- [ ] **Step 1: 写失败测试（目录）** —— `test_model_catalog.py` 追加：

```python
from expert_work.protocol.model_catalog import catalog_entry


def _e(provider: str, name: str):
    entry = catalog_entry(provider, name)
    assert entry is not None, name
    return entry


def test_output_cap_fields_match_live_probe_2026_09_24() -> None:
    # spec §8.1 / §8.7 —— 设错字段在 GLM / DeepSeek 上是静默的,这张表只能靠测试守。
    for name in ("glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-4.7", "glm-4.6",
                 "glm-5v-turbo", "glm-4.6v", "glm-4.5v"):
        assert _e("glm", name).output_cap_field == "max_tokens"
    for name in ("deepseek-v4-pro", "deepseek-v4-flash"):
        assert _e("deepseek", name).output_cap_field == "max_tokens"
    for name in ("kimi-k3", "kimi-k2.6", "kimi-k2.5"):
        assert _e("kimi", name).output_cap_field == "max_completion_tokens"
    for name in ("qwen3.8-max", "qwen3.7-max", "qwen3.6-plus", "qwen3.5-plus"):
        assert _e("qwen", name).output_cap_field == "max_completion_tokens"
    for name in ("qwen3-max", "qwen3-vl-plus", "qwen3-vl-flash"):
        assert _e("qwen", name).output_cap_field == "split"
    for name in ("doubao-seed-2-1-pro-260628", "doubao-seed-2.0-pro", "doubao-seed-2.0-lite"):
        assert _e("doubao", name).output_cap_field == "max_completion_tokens"
    for name in ("gpt-5.5", "gpt-5.5-pro", "gpt-5.4-mini"):
        assert _e("openai", name).output_cap_field == "max_completion_tokens"
    assert _e("anthropic", "claude-opus-4-8").output_cap_field == "max_tokens"


def test_max_output_tokens_match_vendor_ranges() -> None:
    for name in ("glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-4.7", "glm-4.6", "glm-5v-turbo"):
        assert _e("glm", name).max_output_tokens == 131_072
    assert _e("glm", "glm-4.6v").max_output_tokens == 32_768
    assert _e("glm", "glm-4.5v").max_output_tokens == 16_384
    assert _e("deepseek", "deepseek-v4-pro").max_output_tokens == 393_216
    assert _e("deepseek", "deepseek-v4-flash").max_output_tokens == 393_216
    assert _e("doubao", "doubao-seed-2-1-pro-260628").max_output_tokens == 262_144
    assert _e("kimi", "kimi-k3").max_output_tokens is None
    assert _e("qwen", "qwen3.8-max").max_output_tokens is None


def test_thinking_cap_only_on_qwen_thinking_models() -> None:
    capped = {"qwen3.8-max", "qwen3.7-max", "qwen3.6-plus", "qwen3.5-plus", "qwen3-max",
              "qwen3-vl-plus", "qwen3-vl-flash"}
    from expert_work.protocol.model_catalog import MODEL_CATALOG
    for provider, entries in MODEL_CATALOG.items():
        for e in entries:
            assert e.thinking_cap is (provider == "qwen" and e.name in capped), e.name


def test_catalog_corrections_from_live_probe() -> None:
    # glm-5.3 关思考 400(「该模型始终思考」)。
    assert _e("glm", "glm-5.3").always_thinking is True
    # 豆包 budget_tokens 被厂商忽略 → 档位走 reasoning_effort;没有 max 档。
    for name in ("doubao-seed-2-1-pro-260628", "doubao-seed-2.0-pro", "doubao-seed-2.0-lite"):
        e = _e("doubao", name)
        assert e.thinking == "effort"
        assert e.effort_map == {"max": "high"}
    # glm-4.6v / 4.5v 实际默认思考、disabled 有效。
    for name in ("glm-4.6v", "glm-4.5v"):
        e = _e("glm", name)
        assert (e.thinking, e.thinking_default) == ("toggle", True)
    # qwen3-vl 默认不思考、enable_thinking 有效。
    for name in ("qwen3-vl-plus", "qwen3-vl-flash"):
        e = _e("qwen", name)
        assert (e.thinking, e.thinking_default) == ("budget", False)


def test_max_high_low_effort_map_moved_into_catalog() -> None:
    mhl = {"medium": "high"}
    for name in ("glm-5.3", "glm-5.3-flash", "glm-5.2"):
        assert _e("glm", name).effort_map == mhl
    assert _e("kimi", "kimi-k3").effort_map == mhl
```

（若 `catalog_entry` 不在 `model_catalog` 模块，按 `agent_factory.py` 顶部的 import 路径改；glm-5.2 是否当前带 `thinking="effort"` 以文件为准 —— `_MAX_HIGH_LOW_EFFORT` 今天作用于 provider=glm 的所有 effort 条目，effort_map 要落在**每一个** `thinking="effort"` 的 glm 条目上。）

- [ ] **Step 2: 跑，确认红** —— `uv run --no-sync pytest packages/expert-work-protocol/tests/test_model_catalog.py -q` → 字段不存在报错。

- [ ] **Step 3: 实现目录** ——
  - `ModelEntry` 加字段（带注释，说明「实测为准、设错在 GLM / DeepSeek 上静默」）：

```python
    # B-105 —— 请求里用哪个字段表示「思考 + 回答合计」的输出上限(2026-09-24 测试集群实调)。
    #   "max_tokens" / "max_completion_tokens" —— 该字段就是合计上限;
    #   "split" —— 没有合计字段(max_tokens 只管回答):开思考时发 thinking_budget=T、
    #              max_tokens=cap−T 拼出合计,关思考时发 max_tokens=cap。
    # **设错在 GLM / DeepSeek 上是静默的**(200 正常返回,上限不生效),改这里必须实调。
    output_cap_field: Literal["max_tokens", "max_completion_tokens", "split"] = "max_tokens"
    # B-105 —— 厂商接受的输出上限最大值(实调:发超大值看报错给的范围)。None = 厂商不报范围,不校验。
    max_output_tokens: int | None = None
    # B-105 —— 支持独立的思考长度硬上限(通义 thinking_budget,实调:设 200 思考停在 200 后照常作答)。
    thinking_cap: bool = False
    # B-105 —— 平台档位 → 厂商档位取值;缺的键按平台档位原样发。None = 全部原样。
    effort_map: dict[str, str] | None = None
```

  - 按 Global Constraints 与 Step 1 的测试给各条目填值、做修正（glm-5.3 `always_thinking=True`；豆包三款 `thinking="effort"`、`effort_map={"max": "high"}`；glm-4.6v / 4.5v `thinking="toggle", thinking_default=True`；qwen3-vl 两款 `thinking="budget", thinking_default=False`；glm 各 effort 条目与 kimi-k3 `effort_map={"medium": "high"}`；OpenAI 三款只加 `output_cap_field="max_completion_tokens"`，档位不动）。
  - 模块 docstring 追加「上架规矩」一段：新增或修改带思考 / 视觉的模型，必须写明思考形态、默认开关、`output_cap_field`、`max_output_tokens`，并用实调探针（开思考 + cap=300、关思考、cap=10,000,000）核过再合并；探针模板在 spec §8。

- [ ] **Step 4: 跑目录测试转绿**；再跑 `uv run --no-sync pytest packages/expert-work-protocol -q` 看是否有别的测试断言了旧取值（例如豆包 `thinking == "budget"`），按新实测更新，**并在提交说明里列出改了哪些旧断言、为什么**。

- [ ] **Step 5: 写失败测试（factory 档位 / 思考上限）** —— `test_agent_factory.py` 追加（复用文件里的 `_vendor_model` / `_anthropic_model` / `_build_provider`）：

```python
def test_doubao_effort_goes_to_reasoning_effort() -> None:
    from orchestrator.agent_factory import _thinking_payload

    m = "doubao-seed-2-1-pro-260628"
    assert _thinking_payload(_vendor_model("doubao", m, effort="low")) == {"reasoning_effort": "low"}
    assert _thinking_payload(_vendor_model("doubao", m, effort="max")) == {"reasoning_effort": "high"}
    # 关思考仍是真关(实测 thinking.type=disabled 思考 0)。
    assert _thinking_payload(_vendor_model("doubao", m, thinking_enabled=False)) == {
        "thinking": {"type": "disabled"}
    }


def test_glm_53_off_floors_at_low_instead_of_400() -> None:
    from orchestrator.agent_factory import _thinking_payload

    assert _thinking_payload(_vendor_model("glm", "glm-5.3", thinking_enabled=False)) == {
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
    }


def test_qwen_thinking_max_tokens_is_the_budget() -> None:
    from orchestrator.agent_factory import _thinking_payload

    payload = _thinking_payload(
        _vendor_model("qwen", "qwen3.8-max", effort="low", thinking_max_tokens=2000)
    )
    assert payload == {"enable_thinking": True, "thinking_budget": 2000}
    # 只填思考上限、没设档位 = 开思考并限长。
    assert _thinking_payload(_vendor_model("qwen", "qwen3.8-max", thinking_max_tokens=500)) == {
        "enable_thinking": True,
        "thinking_budget": 500,
    }


def test_thinking_max_tokens_rejected_where_unsupported() -> None:
    with pytest.raises(AgentFactoryError, match="只能调思考档位"):
        _build_provider(_vendor_model("glm", "glm-5.3", thinking_max_tokens=1000), "k")
    with pytest.raises(AgentFactoryError, match="只能调思考档位"):
        _build_provider(_anthropic_model(thinking_max_tokens=1000), "k")
    # 目录外不闸(能力未知,和 effort 闸门同一口径),但也不发。
    ok = _build_provider(_vendor_model("qwen", "custom-gw", thinking_max_tokens=1000), "k")
    assert isinstance(ok, OpenAIProvider) and ok.thinking_payload is None
```

  同时更新受影响的旧测试：`test_thinking_payload_budget_vendors` 里的豆包断言改为 `reasoning_effort` 形态；`test_thinking_payload_force_off_per_vendor` 里 `doubao-seed-2.0-pro` 的断言不变（仍是 disabled）；`test_thinking_toggle_gate_on_no_knob_model` 里的 `qwen3-vl-plus` 换成目录里仍 `thinking=None` 的模型（例如 `glm-4-plus`）。

- [ ] **Step 6: 跑，确认红**：`uv run --no-sync pytest services/orchestrator/tests/test_agent_factory.py -q -k "thinking or doubao or glm_53"`。

- [ ] **Step 7: 实现 factory** ——
  - 删 `_MAX_HIGH_LOW_EFFORT`，新增：

```python
def _vendor_effort(entry: ModelEntry, level: str) -> str:
    """平台档位 → 厂商取值(B-105:映射登记在目录 ``effort_map``,缺的键原样发)。"""
    return (entry.effort_map or {}).get(level, level)
```

  - `_thinking_enable_payload` 的 effort 分支：glm / kimi 用 `_vendor_effort(entry, model.effort)` 替掉 `_MAX_HIGH_LOW_EFFORT[...]`；新增豆包分支（放在通用 `reasoning_effort` 之前）：`effort is None` → 保持 `{"thinking": {"type": "auto"}}`（与今天一致），否则 `{"reasoning_effort": _vendor_effort(entry, model.effort)}`；通用分支也走 `_vendor_effort`（OpenAI 无 effort_map → 原样，行为不变）。
  - budget 分支（此后只剩通义）：

```python
        if model.thinking_max_tokens is not None:
            return {"enable_thinking": True, "thinking_budget": model.thinking_max_tokens}
        if model.effort is None:
            return {"enable_thinking": True}
        return {
            "enable_thinking": True,
            "thinking_budget": _thinking_budget(model.effort, model.max_tokens),
        }
```

  - `_thinking_payload` 的「继承」分支：`model.thinking_max_tokens is not None` 也视为「碰过算力旋钮」（与 effort 同列），否则只填思考上限的 manifest 什么都不发。
  - `_thinking_disable_payload` 的豆包：catalog 改成 effort 后会落到 effort 分支 —— 在 effort 分支里加 `if model.provider == "doubao": return {"thinking": {"type": "disabled"}}`（放在 `always_thinking` 判断之后、`glm/deepseek` 之前），保证关思考线格式不变。
  - `_build_provider` 两处闸门（anthropic 分支与 compat 分支，紧挨已有 effort 闸门）：

```python
    if model.thinking_max_tokens is not None and entry is not None and not entry.thinking_cap:
        raise AgentFactoryError(
            f"model {model.name!r}: 该模型只能调思考档位,不能限制思考长度;"
            "remove model.thinking_max_tokens from the manifest"
        )
```

  - `ModelSpec` 加字段（放在 `thinking_enabled` 之后）：

```python
    #: B-105 —— 思考长度硬上限(token)。只在目录 ``thinking_cap=True`` 的模型上可用(实调:
    #: 通义 ``thinking_budget``),其他模型填了构建即报错。与 ``max_tokens`` 脱钩,不再按比例推。
    thinking_max_tokens: int | None = Field(default=None, gt=0)
```

- [ ] **Step 8: 跑转绿**：`uv run --no-sync pytest services/orchestrator/tests/test_agent_factory.py packages/expert-work-protocol/tests -q`；再跑 `uv run --no-sync pytest services/orchestrator/tests -q -k "thinking or effort or escalat" ` 看升级阶梯（`_escalated_model`）是否因豆包形态变化受影响，按新形态修测试断言。

- [ ] **Step 9: 变异自证** —— 把 `glm-5.3` 的 `always_thinking=True` 临时删掉，确认 `test_glm_53_off_floors_at_low_instead_of_400` 红；把豆包 `effort_map` 删掉，确认 `test_doubao_effort_goes_to_reasoning_effort` 红；各自还原后 `git diff` 确认还原干净。

- [ ] **Step 10: Commit**

```bash
git add packages/expert-work-protocol services/orchestrator/src/orchestrator/agent_factory.py services/orchestrator/tests/test_agent_factory.py
git commit -m "feat(b105): 目录登记输出上限字段/厂商上限/思考上限能力,档位映射进目录,修 glm-5.3 关思考 400 与豆包档位不生效"
```

---

### Task 2: 输出上限按目录发送 + 旧默认值归一

**Files:**
- Modify: `packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py:108`（`max_tokens` 可空 + before-validator）
- Modify: `services/orchestrator/src/orchestrator/agent_factory.py`（新增 `_output_cap_payload`；`_build_provider` 各分支传入；Anthropic 空值 → 4096；上限校验；`_thinking_budget` 基数）
- Modify: `services/orchestrator/src/orchestrator/llm/providers/openai.py:470-537`（`OpenAIProvider.output_cap_payload` 字段，`_prepare_request` 合并）
- Modify: `services/orchestrator/src/orchestrator/middleware_assembly.py:133,141`（`model.max_tokens or 0`）
- Modify: `services/control-plane/src/control_plane/seed_canary.py:112,117`（删两处 `max_tokens: 4096`）
- Modify: `docs/architecture/02-AGENT-MANIFEST.md:64`（示例与说明）
- Test: `services/orchestrator/tests/test_agent_factory.py`、`services/orchestrator/tests/test_llm_provider_openai.py`、`packages/expert-work-protocol/tests/`（agent_spec 测试文件，按现有文件名）

**Interfaces:**
- Consumes: Task 1 的 `ModelEntry.output_cap_field / max_output_tokens`、`ModelSpec.thinking_max_tokens`、`_thinking_budget`。
- Produces: `ModelSpec.max_tokens: int | None`；`OpenAIProvider.output_cap_payload: dict[str, Any] | None = None`；`_output_cap_payload(model: ModelSpec, entry: ModelEntry | None) -> dict[str, Any] | None`。Task 4 前端依赖 `max_tokens` 可空。

- [ ] **Step 1: 写失败测试（归一）**

```python
from expert_work.protocol.agent_spec import ModelSpec


def test_legacy_default_4096_normalises_to_none_off_anthropic() -> None:
    assert ModelSpec.model_validate({"provider": "glm", "name": "glm-5.3", "max_tokens": 4096}).max_tokens is None
    assert ModelSpec.model_validate({"provider": "glm", "name": "glm-5.3"}).max_tokens is None
    # 用户自己设的值保留。
    assert ModelSpec.model_validate({"provider": "glm", "name": "glm-5.3", "max_tokens": 40960}).max_tokens == 40960
    # Anthropic 的 4096 一直生效,原样保留。
    assert ModelSpec.model_validate({"provider": "anthropic", "name": "claude-opus-4-8", "max_tokens": 4096}).max_tokens == 4096
    # fallback 节点同样归一。
    spec = ModelSpec.model_validate({
        "provider": "glm", "name": "glm-5.3",
        "fallback": [{"provider": "qwen", "name": "qwen3.8-max", "max_tokens": 4096}],
    })
    assert spec.fallback[0].max_tokens is None
```

- [ ] **Step 2: 写失败测试（发送）** —— `test_agent_factory.py`：

```python
def _cap_payload(provider: str, name: str, **kw: Any) -> dict[str, Any] | None:
    p = _build_provider(_vendor_model(provider, name, **kw), "k")
    assert isinstance(p, OpenAIProvider)
    return p.output_cap_payload


def test_no_cap_sends_nothing_on_every_compat_vendor() -> None:
    for provider, name in (("glm", "glm-5.3"), ("deepseek", "deepseek-v4-pro"), ("kimi", "kimi-k3"),
                           ("qwen", "qwen3.8-max"), ("qwen", "qwen3-max"),
                           ("doubao", "doubao-seed-2-1-pro-260628"), ("openai", "gpt-5.5")):
        assert _cap_payload(provider, name) is None, name


def test_cap_goes_to_the_catalog_field_only() -> None:
    assert _cap_payload("glm", "glm-5.3", max_tokens=8000) == {"max_tokens": 8000}
    assert _cap_payload("deepseek", "deepseek-v4-pro", max_tokens=8000) == {"max_tokens": 8000}
    assert _cap_payload("qwen", "qwen3.8-max", max_tokens=8000) == {"max_completion_tokens": 8000}
    doubao = _cap_payload("doubao", "doubao-seed-2-1-pro-260628", max_tokens=8000)
    assert doubao == {"max_completion_tokens": 8000}  # 绝不同时带 max_tokens(实测 400)
    assert _cap_payload("kimi", "kimi-k3", max_tokens=8000) == {"max_completion_tokens": 8000}
    assert _cap_payload("openai", "gpt-5.5", max_tokens=8000) == {"max_completion_tokens": 8000}
    # 目录外:openai / azure 按 OpenAI 语义,其余 max_tokens。
    assert _cap_payload("qwen", "custom-gw", max_tokens=8000) == {"max_tokens": 8000}
    assert _cap_payload("openai", "my-ft-model", max_tokens=8000) == {"max_completion_tokens": 8000}


def test_split_without_cap_sends_nothing() -> None:
    assert _cap_payload("qwen", "qwen3-max", thinking_enabled=True) is None


def test_split_with_thinking_on_sums_to_cap() -> None:
    # 用户填了思考上限:T=2000,回答=cap−T。
    assert _cap_payload("qwen", "qwen3-max", max_tokens=10_000, thinking_enabled=True,
                        thinking_max_tokens=2000) == {"max_tokens": 8000, "thinking_budget": 2000}
    # 没填思考上限:T=cap×档位比例(没设档位按 high 0.8)。
    assert _cap_payload("qwen", "qwen3-vl-plus", max_tokens=10_000, thinking_enabled=True) == {
        "max_tokens": 2000, "thinking_budget": 8000,
    }
    assert _cap_payload("qwen", "qwen3-vl-plus", max_tokens=10_000, effort="low") == {
        "max_tokens": 8000, "thinking_budget": 2000,
    }


def test_split_with_thinking_off_is_plain_max_tokens() -> None:
    assert _cap_payload("qwen", "qwen3-vl-flash", max_tokens=3000, thinking_enabled=False) == {
        "max_tokens": 3000
    }
    # qwen3-vl 默认不思考(thinking_default=False),未碰开关 = 关。
    assert _cap_payload("qwen", "qwen3-vl-flash", max_tokens=3000) == {"max_tokens": 3000}


def test_split_thinking_cap_must_be_below_output_cap() -> None:
    with pytest.raises(AgentFactoryError, match="思考长度上限必须小于输出上限"):
        _build_provider(_vendor_model("qwen", "qwen3-max", max_tokens=2000,
                                      thinking_enabled=True, thinking_max_tokens=2000), "k")


def test_cap_above_vendor_max_is_rejected() -> None:
    with pytest.raises(AgentFactoryError, match="16384"):
        _build_provider(_vendor_model("glm", "glm-4.5v", max_tokens=20_000), "k")
    # 无公布上限的模型不校验。
    assert _cap_payload("kimi", "kimi-k3", max_tokens=5_000_000) == {"max_completion_tokens": 5_000_000}


def test_anthropic_empty_cap_keeps_4096() -> None:
    p = _build_provider(_anthropic_model(max_tokens=None), "k")
    assert isinstance(p, AnthropicProvider) and p.max_tokens == 4096


def test_fallback_nodes_use_their_own_cap_field() -> None:
    from orchestrator.agent_factory import _output_cap_payload
    from expert_work.protocol.model_catalog import catalog_entry

    primary = _vendor_model("glm", "glm-5.3", max_tokens=9000,
                            fallback=[{"provider": "qwen", "name": "qwen3.8-max", "max_tokens": 9000}])
    fb = primary.fallback[0]
    assert _output_cap_payload(primary, catalog_entry("glm", "glm-5.3")) == {"max_tokens": 9000}
    assert _output_cap_payload(fb, catalog_entry("qwen", "qwen3.8-max")) == {"max_completion_tokens": 9000}
```

  `test_llm_provider_openai.py`：`output_cap_payload={"max_tokens": 123}` 时，`complete()` 与 `stream()` 两条路径发出的请求体都带 `max_tokens=123`；同时有 `thinking_payload` 时两者合并，键冲突（split 的 `thinking_budget`）以 `output_cap_payload` 为准；两者都为 None 时请求体与今天逐字节相同（沿用文件里已有的「抓请求体」fixture）。

- [ ] **Step 3: 跑，确认红**。

- [ ] **Step 4: 实现** ——
  - `ModelSpec`：

```python
    #: B-105 —— 单次输出上限,**含思考**(平台语义:思考 + 回答合计)。``None`` = 请求里
    #: 不带上限、用厂商默认;Anthropic 必须带上限,为空时发 4096。按目录 ``output_cap_field``
    #: 发到该厂商真正表示合计的字段。注意:非 Anthropic 模型上的 4096 在加载时归一成空
    #: (旧默认值被表单写回、从未生效,与「没设」分不出来);需要这个数请填 4095 / 4097。
    max_tokens: int | None = Field(default=None, gt=0)
```

  并加 before-validator（`@model_validator(mode="before")`，只处理 dict 输入；`provider != "anthropic" and data.get("max_tokens") == 4096` → 复制一份删掉该键；fallback 节点是嵌套 ModelSpec，会各自走一遍）。
  - `_thinking_budget(effort, max_tokens)`：`max_tokens` 为 None 时基数用 `_THINKING_BUDGET_MAX`（比例 × 81920 再夹紧）—— 与今天「4096 × 比例」相比，这是**通义**在「只设档位、没设上限」时的预算变化，在注释里写明，并更新对应旧测试。
  - 新增 `_output_cap_payload`：

```python
_LEGACY_ANTHROPIC_MAX_TOKENS = 4096


def _output_cap_payload(model: ModelSpec, entry: ModelEntry | None) -> dict[str, Any] | None:
    """B-105 —— 输出上限(含思考)的请求字段;``None`` = 不带上限(厂商默认)。

    字段取目录 ``output_cap_field``(实调结果),只发一个 —— 豆包同时发两个会 400,
    GLM / DeepSeek 发错字段会被静默忽略。目录外模型按 ``max_tokens`` 发。
    """
    cap = model.max_tokens
    if cap is None:
        return None
    if entry is not None:
        field = entry.output_cap_field
    else:
        # 目录外:OpenAI 推理模型拒收 max_tokens;Azure 部署名永远不在目录里,按 OpenAI 语义。
        field = "max_completion_tokens" if model.provider in ("openai", "azure") else "max_tokens"
    if field != "split":
        return {field: cap}
    if not _thinking_on(model, entry):
        return {"max_tokens": cap}
    budget = model.thinking_max_tokens
    if budget is None:
        budget = int(cap * _THINKING_BUDGET_RATIO[model.effort or "high"])
    return {"max_tokens": cap - budget, "thinking_budget": budget}
```

  `_thinking_on(model, entry)`：`thinking_enabled` 显式值优先；否则「碰过旋钮」（effort / adaptive / thinking_max_tokens 任一）为开；否则取 `entry.thinking_default`。
  - `_build_provider`：compat 分支取 `compat_entry` 后做两条校验（放在已有闸门旁）：`max_output_tokens` 不为 None 且 cap 超出 → `AgentFactoryError(f"model {name!r}: 输出上限 {cap} 超过厂商上限 {max_output_tokens}")`；`output_cap_field == "split"` 且 `thinking_max_tokens >= cap` → `AgentFactoryError("…思考长度上限必须小于输出上限…")`。每个构造 `OpenAIProvider` / `OpenAICompatibleProvider` 的地方（openai、compat 五家、self-hosted、azure 四处）传 `output_cap_payload=_output_cap_payload(model, compat_entry)`。anthropic 分支传 `max_tokens=model.max_tokens or _LEGACY_ANTHROPIC_MAX_TOKENS`，同样做上限校验。
  - `OpenAIProvider`：加字段 `output_cap_payload: dict[str, Any] | None = None`（注释：B-105，None 时请求体逐字节不变）；`_prepare_request` 的 `extra_body` 改为：

```python
            "extra_body": _merge_extra(self.thinking_payload, self.output_cap_payload),
```

  其中 `_merge_extra(a, b)`：两者都 None → None；否则 `{**(a or {}), **(b or {})}`（后者优先 —— split 的 `thinking_budget` 覆盖思考翻译里按比例推的那个）。
  - `middleware_assembly.py` 两处：`max_tokens=model.max_tokens or 0`（注释：0 = 厂商默认，只用于缓存键）。
  - `seed_canary.py` 删两处 `max_tokens: 4096`；`02-AGENT-MANIFEST.md` 示例改为注释说明「可省略；省略 = 厂商默认；含思考」。

- [ ] **Step 5: 跑转绿**：`uv run --no-sync pytest services/orchestrator/tests packages/expert-work-protocol/tests services/control-plane/tests -q -x --timeout 120 -p no:cacheprovider -k "not integration"`；全部 `max_tokens` 相关旧断言（`git grep -n "max_tokens" -- '*test*'`）逐个过一遍：依赖「默认 4096」的改成新语义，并在提交说明里列出。

- [ ] **Step 6: 变异自证** —— 把 `_output_cap_payload` 里 `field != "split"` 分支改成总发 `max_tokens`，确认 `test_cap_goes_to_the_catalog_field_only` 红；把 before-validator 的 `== 4096` 改成 `== 4095`，确认归一测试红；还原并 `git diff` 验干净。

- [ ] **Step 7: mypy** —— `uv run --no-sync mypy services/orchestrator/src packages/expert-work-protocol/src`（`max_tokens` 变可空后，任何把它当 int 用的地方都会在这里暴露）。

- [ ] **Step 8: Commit**

```bash
git commit -am "feat(b105): 输出上限(含思考)按目录字段真正发送;非 Anthropic 旧默认 4096 加载时归一为厂商默认"
```

---

### Task 3: 截断可见 + 看图快看被拒时退回正常看图

**Files:**
- Create: `services/orchestrator/src/orchestrator/llm/truncation.py`
- Modify: `services/orchestrator/src/orchestrator/llm/providers/anthropic.py:~1008`（`_from_anthropic_response` 写 `response_metadata["stop_reason"]`）
- Modify: `services/orchestrator/src/orchestrator/llm/providers/_streaming.py:~355`（Anthropic assembler `build` 把 `self._finish` 放进 body 的 `stop_reason`）
- Modify: `services/orchestrator/src/orchestrator/graph_builder/builder.py:~1177`（B3 记账之后判定截断）
- Modify: `services/orchestrator/src/orchestrator/tools/vision.py:~218`（快看被拒退回）
- Test: `services/orchestrator/tests/test_llm_truncation.py`（新）、`services/orchestrator/tests/test_ask_image_depth_timeout.py`、agent 节点的现有集成测试文件（按 `ContextOverflowError` 的 run 失败测试所在文件，用它的 fixture）

**Interfaces:**
- Produces: `is_truncated(message: AIMessage) -> bool`、`is_unusable_truncation(message: AIMessage) -> bool`、`class OutputTruncatedError(RuntimeError)`（属性 `cap: int | None`）、计数器 `expert_work_llm_output_truncated_total{provider,model,usable}` 与 `expert_work_vision_quick_fallback_total{model}`。

- [ ] **Step 1: 写失败测试（判定）** —— `test_llm_truncation.py`：

```python
from langchain_core.messages import AIMessage

from orchestrator.llm.truncation import is_truncated, is_unusable_truncation


def _msg(content: str = "", *, finish: str | None = None, stop: str | None = None, tools: bool = False) -> AIMessage:
    meta: dict[str, str] = {}
    if finish:
        meta["finish_reason"] = finish
    if stop:
        meta["stop_reason"] = stop
    calls = [{"id": "c1", "name": "write_file", "args": {}, "type": "tool_call"}] if tools else []
    return AIMessage(content=content, tool_calls=calls, response_metadata=meta)


def test_openai_length_and_anthropic_max_tokens_are_truncation() -> None:
    assert is_truncated(_msg("x", finish="length"))
    assert is_truncated(_msg("x", stop="max_tokens"))
    assert not is_truncated(_msg("x", finish="stop"))
    assert not is_truncated(_msg("x", stop="end_turn"))
    assert not is_truncated(_msg("x"))


def test_empty_text_after_truncation_is_unusable() -> None:
    assert is_unusable_truncation(_msg("", finish="length"))
    assert is_unusable_truncation(_msg("   ", finish="length"))


def test_truncated_with_tool_calls_is_unusable() -> None:
    # 最后一个工具调用的参数必然被截,_parse_arguments 会静默给 {}。
    assert is_unusable_truncation(_msg("好的,我来写文件", finish="length", tools=True))


def test_truncated_text_only_is_usable() -> None:
    assert not is_unusable_truncation(_msg("一段完整度够用的回答", finish="length"))
    assert not is_unusable_truncation(_msg("", finish="stop"))
```

  另加 Anthropic 解析测试（放在 anthropic provider 现有测试文件里）：非流式 body 带 `"stop_reason": "max_tokens"` → `response_metadata["stop_reason"] == "max_tokens"`；流式 assembler 收到 stop_reason delta 后 `build()` 出的消息同样带上。

- [ ] **Step 2: 跑，确认红**。

- [ ] **Step 3: 实现 `truncation.py`**：

```python
"""B-105 —— 输出被上限截断的判定与报错。

各家形态(2026-09-24 实调):OpenAI 兼容 ``finish_reason == "length"``;Anthropic
``stop_reason == "max_tokens"``。思考模型截断时通常是思考吃满额度、正文为空。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

_TRUNCATED = {("finish_reason", "length"), ("stop_reason", "max_tokens")}


def is_truncated(message: AIMessage) -> bool:
    meta = message.response_metadata or {}
    return any(meta.get(key) == value for key, value in _TRUNCATED)


def is_unusable_truncation(message: AIMessage) -> bool:
    """截断且答案不能用:正文为空,或带着工具调用(最后一个的参数必然被截)。"""
    if not is_truncated(message):
        return False
    if message.tool_calls or message.invalid_tool_calls:
        return True
    text = message.content if isinstance(message.content, str) else str(message.content or "")
    return not text.strip()


class OutputTruncatedError(RuntimeError):
    """模型输出被上限截断且答案不可用。在 agent 节点、路由之后抛出 —— 不触发 fallback
    (备用模型也是同一个上限),run 以可见错误结束。"""

    def __init__(self, cap: int | None) -> None:
        shown = str(cap) if cap is not None else "厂商默认值"
        super().__init__(
            f"模型输出被截断(已用满输出上限 {shown},含思考)。"
            "请在模型配置里调大『输出上限』,或者降低思考档位。"
        )
        self.cap = cap
```

  （若 `message.content` 是块列表，按 builder 里已有的取文字 helper 取文字，别自己再写一个 —— 先 `git grep -n "def _stringify\|def _message_text" services/orchestrator/src` 找现成的。）

- [ ] **Step 4: Anthropic 两处写 stop_reason** —— `_from_anthropic_response` 读 `body.get("stop_reason")`，是非空字符串就放进 `AIMessage(response_metadata={"stop_reason": ...})`（两个 return 都带）；assembler `build` 里 `if self._finish: body["stop_reason"] = self._finish`。

- [ ] **Step 5: agent 节点接线** —— builder.py 在「B3 — count the INITIAL response」那段 `token_budget.add(...)` 之后插入：

```python
        # B-105 —— 截断:答案不可用就让 run 可见地失败(在路由之后,不触发 fallback);
        # 正文可用照常交付,只计数。缓存命中的旧回答不再判(存进缓存前已判过)。
        if cache_hit_response is None and is_truncated(response):
            usable = not is_unusable_truncation(response)
            output_truncated_total.labels(
                provider=model_provider, model=model_name, usable=str(usable).lower()
            ).inc()
            if not usable:
                raise OutputTruncatedError(configured_max_tokens)
            logger.warning("agent_node.output_truncated_usable model=%s", model_name)
```

  `model_provider` / `model_name` / `configured_max_tokens` 从 `build_agent_graph` 已有的 model 入参闭包取（先看 builder 的签名里 manifest 的 `ModelSpec` 从哪进来；没有就在 factory 调用 builder 处多传一个 `output_cap: int | None` 参数，默认 None）。计数器用 `expert_work_counter` 在 `truncation.py` 定义（`("provider", "model", "usable")`）。
  - 写集成测试：用 builder 现有的桩 LLMCaller fixture，返回 `AIMessage(content="", response_metadata={"finish_reason": "length"})` → run 失败，失败原因文案包含「模型输出被截断」；返回带文字的 length 回复 → run 正常结束、回答照常。**先确认失败文案真的到达 run 的错误帧 / RUN_FAILED 记录**（照 `ContextOverflowError` 的现有测试怎么断言就怎么断言）；如果那条路径把异常文案吞成通用错误，就在同一处把 `OutputTruncatedError` 的文案透出，并补一条断言。

- [ ] **Step 6: 快看退回** —— `vision.py`：

```python
        if depth == "quick" and self.quick_vl_caller is not None:
            try:
                with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "vision"):
                    response = await self._call_with_limit(
                        self.quick_vl_caller, messages, depth=depth, ctx=ctx
                    )
            except LLMClientError as exc:
                if not _quick_rejected(exc):
                    raise
                # B-105 —— 关思考的参数被这家拒了(目录没实调过的新模型最常见):退回 Agent
                # 原本的看图配置再看一次,而不是让看图整个失败。
                vision_quick_fallback_total.labels(model=...).inc()
                logger.warning("ask_image.quick_rejected_fallback err=%s", type(exc).__name__)
                with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "vision"):
                    response = await self._call_with_limit(
                        self.vl_caller, messages, depth=depth, ctx=ctx
                    )
        else:
            ...原逻辑...
```

  `_quick_rejected(exc)`：是 `LLMClientError` 且**不是** `LLMUnauthorizedError`；若路由把 4xx 包成 `AllProvidersExhaustedError`，先看 `router.py` 实际行为 —— 4xx 在路由里是不 fallback 直接抛出，还是被包装；按实际情况解包 `last_exc`，并写测试钉住。429 是 `LLMRateLimitError`（不继承 `LLMClientError`）天然不进这个分支。model 标签取 VL 主模型名（`vision_block.model.name`，在 factory 构造 `AskImageTool` 时多传一个 `vl_model_name: str = ""` 字段）。
  - 测试（`test_ask_image_depth_timeout.py` 里用它现有的桩 caller）：快看 caller 抛 `LLMClientError("400 bad param")` → 走 `vl_caller`，工具成功，回答来自 `vl_caller`；抛 `LLMUnauthorizedError` → 原样抛出、`vl_caller` 未被调用（`test_quick_fallback_skips_auth_and_rate_limit`，同时覆盖 `LLMRateLimitError`）；`depth="deep"` 抛 `LLMClientError` → 原样抛出（只有快看退回）。

- [ ] **Step 7: 跑转绿** + 变异自证：把 `is_unusable_truncation` 的 `tool_calls` 判断删掉 → `test_truncated_with_tool_calls_is_unusable` 红；把 `_quick_rejected` 改成恒 True → 401 测试红；还原并验干净。

- [ ] **Step 8: Commit**

```bash
git commit -am "feat(b105): 截断成不可用回答时 run 可见失败;看图快看被厂商拒时退回正常看图"
```

---

### Task 4: 配置页 —— 输出上限（含思考）+ 思考长度上限

**Files:**
- Modify: `apps/admin-ui/src/api/model_catalog.ts`（`CatalogModel` 加 `output_cap_field?`、`max_output_tokens?`、`thinking_cap?`）
- Modify: `apps/admin-ui/src/components/manifest-editor/form_model.ts`（`ModelFields` 加 `thinking_max_tokens?: number`；序列化 / 反序列化同 `context_window` 的处理方式，空 = 不写键）
- Modify: `apps/admin-ui/src/components/manifest-editor/widgets/ModelSelect.tsx:180-205`（输出上限字段改标签 + 占位；新增思考长度上限字段）
- Modify: `apps/admin-ui/src/i18n/locales/zh-CN.ts`、`en.ts`（`model_select` 段）
- Test: `ModelSelect` 现有 vitest 文件（`git ls-files apps/admin-ui | grep -i modelselect`）

**Interfaces:**
- Consumes: 后端 `/v1/model-catalog` 已经 `model_dump` 整个 `ModelEntry`，Task 1 的新字段自动带出，不用改后端路由。

- [ ] **Step 1: 写失败测试**（按该测试文件现有的 render 助手与 catalog 桩）：
  1. 选 `glm-4.5v`、`max_tokens` 为空 → 输出上限输入框的占位文字包含「厂商默认」和「16384」。
  2. 选 `qwen3.8-max` → 「思考长度上限」输入框可编辑，输入 2000 → `onChange` 收到 `thinking_max_tokens: 2000`。
  3. 选 `glm-5.3` → 「思考长度上限」输入框 disabled，页面有「该模型只能调思考档位，不能限制思考长度。」。
  4. 从 `qwen3.8-max`（已填 2000）切到 `glm-5.3` → `onChange` 收到的值里没有 `thinking_max_tokens`（切换模型时清掉，否则保存必被后端拒）。
  5. 输出上限标签文字为「输出上限（含思考）」。

- [ ] **Step 2: 跑确认红**：`pnpm -C apps/admin-ui test -- ModelSelect`。

- [ ] **Step 3: 实现** ——
  - i18n（zh-CN）：

```ts
    max_tokens_label: "输出上限（含思考）",
    max_tokens_placeholder: "厂商默认",
    max_tokens_placeholder_max: "厂商默认（最大 {{n}}）",
    max_tokens_hint: "单次输出上限，含思考。留空 = 厂商默认。",
    thinking_max_label: "思考长度上限",
    thinking_max_hint: "思考超过这个长度就停下来直接作答。留空 = 按推理深度。",
    thinking_max_unsupported: "该模型只能调思考档位，不能限制思考长度。",
```

  en 对应：`"Max output (incl. thinking)"`、`"Vendor default"`、`"Vendor default (max {{n}})"`、`"Per-reply output cap, thinking included. Empty = vendor default."`、`"Thinking length cap"`、`"Thinking stops at this length and the model answers. Empty = follow reasoning depth."`、`"This model only supports thinking levels, not a thinking length cap."`。删除旧 `max_tokens_hint` 里「仅 Anthropic 路径生效…」的文案（被新文案替换）。
  - ModelSelect：输出上限 `<span>` 用 `t("model_select.max_tokens_label")`；`InputNumber` 加 `min={1}`、`max={selected?.max_output_tokens ?? undefined}`、`placeholder`（有 `max_output_tokens` 用带数的那条）、`aria-label`、`data-testid="model-select-max-tokens"`。紧挨着「推理深度」之后加「思考长度上限」（只在 `hasThinkingKnob` 时渲染）：`disabled={!selected?.thinking_cap}`，不支持时下方提示用 `thinking_max_unsupported`，`data-testid="model-select-thinking-max"`。切换 provider / model 的 handler 里，新模型 `thinking_cap` 不为 true 时从结果里删掉 `thinking_max_tokens`（与切换时清 `effort` 的现有写法对齐 —— 先看现在切模型时怎么处理 effort，照同一处改）。
- [ ] **Step 4: 跑转绿** + `pnpm -C apps/admin-ui typecheck` + `pnpm -C apps/admin-ui lint`；i18n 死键 / 重复键脚本如仓库有就跑（`git grep -n "i18n" apps/admin-ui/package.json`）。
- [ ] **Step 5: e2e 检查** —— `git grep -n "model-select" apps/admin-ui/e2e` 看是否有 Playwright 用例依赖旧的 max_tokens 文案 / testid，有就同步。
- [ ] **Step 6: Commit**

```bash
git commit -am "feat(b105): 配置页输出上限标明含思考并显示厂商上限;新增思考长度上限,不支持的模型置灰"
```

---

### Task 5（控制器执行，不派子 Agent）：终审 → 测试环境 → 真栈回归

- [ ] 全分支终审（最强模型），关注 Review Focus 五条 + 线格式逐字节不变的承诺（无上限、无旋钮的 manifest 请求体与 main 完全一致）。
- [ ] 开 PR（后端 Task 1–3 + 前端 Task 4 同一个 PR 或两个，按终审时 diff 大小定）；CI 绿后合并；`release.sh test` 发测试环境，开发布记录 PR。
- [ ] 真栈回归（测试集群，探针脚本放 scratchpad `b105-live/`）：
  1. ai-health-plan 不改配置读 PDF 场景复跑一次（它存的 40960 从此开始生效，历史最长 33,522，预期不截断）→ 通过。
  2. 临时探针 Agent，主模型依次设 glm-5.3 / kimi-k3 / deepseek-v4-flash / qwen3.8-max / doubao-seed-2-1-pro，`max_tokens=300` + 开思考 → 每个 run 以「模型输出被截断」可见失败；`expert_work_llm_output_truncated_total` 各 +1。
  3. 同一探针 Agent 设 qwen3-max + `max_tokens=3000` + 开思考 → 正常作答，`token_usage` 该次输出 ≤ 3000（拼合计生效）。
  4. 探针 Agent 在 glm-5.3 上填 `thinking_max_tokens` → 保存被拒，错误含「只能调思考档位」。
  5. 看图：vision 模型设 glm-4.6v，ask_image 快看一页 → 秒级返回（修目录前是 >120s 超时）。
  6. 清理探针 Agent；ROADMAP：B-105 销案、新增 backlog（kimi-k2.5 / doubao-seed-2.0-pro / 2.0-lite 404 → 并入 B-112；OpenAI / Azure / Anthropic 档位与上限待有 key 实调）。
- [ ] 生产前置：给用户一条只读 SQL（统计生产 `agent_spec` 里非 Anthropic 且 `max_tokens` 不为 4096 / 空的模型条目数，按厂商和值分组，以及 Anthropic Agent 数），由用户执行或授权。
