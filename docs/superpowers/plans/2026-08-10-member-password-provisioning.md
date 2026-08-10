# 成员初始密码开通模式(password provisioning)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建租户(含首管)/邀请成员时不再依赖 SMTP:平台开关切到 `password` 模式后,服务端生成可读初始密码、写进 Keycloak(`temporary=True` 首登强制改密),密码只在创建/重发响应里回传一次,admin-ui 弹一次性凭据面板供复制。

**Architecture:** 三条既有链(member invite / member resend / tenant first-admin)共用的 Keycloak 开通函数加一个 `provisioning_mode` 分支:`email` 分支原样(send_setup_email),`password` 分支改为 `create_user(email_verified=True)` + `reset_password(temporary=True)`,生成的密码沿 `MemberOpResult` / `FirstAdminResult` 冒回端点响应。前端纯响应驱动:响应里有 `initial_password` 就弹一次性面板,没有就维持旧文案——前端不感知平台配置。

**Tech Stack:** FastAPI + pydantic Settings、Keycloak Admin API(既有 `KeycloakAdminClient`)、React + antd(admin-ui)、vitest / pytest。

## Global Constraints

- **密码绝不落库、绝不进审计、绝不进日志**——只存在于 Keycloak 和那一次 HTTP 响应体里。任何 `emit(...)` / `logger.*` 调用的参数里出现密码即为缺陷。
- 平台级开关:`Settings.member_provisioning_mode: Literal["email", "password"]`,默认 `"email"`(现状零变化;`password` 模式为 opt-in)。
- `password` 模式下 `create_user` 必须传 `email_verified=True`(无 SMTP 验证不了邮箱,realm 若开强制验证会把人锁死在门外)。
- `reset_password` 必须 `temporary=True`(Keycloak 原生首登强制改密)。
- 密码格式:`word-word-word-NNNN`(3 个词 `secrets.choice` 自内嵌 256 词表 + `-` + 4 位数字),全小写。
- email 分支行为逐字节保持:`send_setup_email` 仍被调用、`email_verified` 仍为 False、响应不含 `initial_password` 有效值(恒 `None`)。
- 失败语义与 email 分支对齐:`reset_password` 抛 `KeycloakUnavailableError` 时不回滚、log warning(不含密码)、`initial_password=None`,resend 可重驱(resend 在 password 模式下重新生成新密码——这就是「重置密码并重新复制」的后端语义)。
- 每条新断言过变异自证(break→red→restore→green),重点杀:「password 模式仍发邮件」「temporary 传成 False」「email_verified 没传 True」「密码进了 audit details」。
- 前端一次性面板必须带「仅显示这一次」警示 + 复制按钮;关闭后无处再查(找回=重发/重置)。
- i18n 新键先查 en/zh-CN 是否撞既有键(同 object 重复键 esbuild 静默覆盖)。
- 本地验证命令:后端 `cd services/control-plane && uv run pytest tests/test_members_api.py tests/test_tenants_api.py tests/test_member_password.py -q`;前端 `cd apps/admin-ui && npx vitest run src/pages/__tests__/SettingsMembers.test.tsx src/components/__tests__/CreateTenantDrawer.test.tsx src/components/__tests__/OneTimeCredentialPanel.test.tsx`(具体测试文件名以任务为准);ruff / tsc 照 CI 范围。

## File Structure

- `services/control-plane/src/control_plane/settings.py` — 开关字段(改)
- `services/control-plane/src/control_plane/member_password.py` — 密码生成器(新,含词表)
- `services/control-plane/src/control_plane/api/member_ops.py` — `_provision_keycloak` 分支 + `MemberOpResult.initial_password`(改)
- `services/control-plane/src/control_plane/api/members.py` — invite/resend 透传 mode + 响应字段(改)
- `services/control-plane/src/control_plane/api/first_admin.py` — 同分支 + `FirstAdminResult.initial_password`(改)
- `services/control-plane/src/control_plane/api/tenants.py` — first_admin 响应字段(改)
- `services/control-plane/src/control_plane/keycloak/fake_admin_client.py` — 记录 reset_password 调用(改,若缺)
- `apps/admin-ui/src/components/OneTimeCredentialPanel.tsx` — 一次性凭据面板(新)
- `apps/admin-ui/src/components/CreateTenantDrawer.tsx` — 成功后弹面板(改)
- `apps/admin-ui/src/pages/SettingsMembers.tsx` — 邀请结果面板 + 重发面板(改)
- `apps/admin-ui/src/api/{tenants,members}.ts` — 响应类型(改)
- `apps/admin-ui/src/i18n/locales/{en,zh-CN}.ts` — 文案(改)
- `infra/k8s/overlays/test/configmap-patch.yaml` — 测试集群开 password 模式(改)
- `docs/runbooks/bootstrap-admin.md` — 模式说明一节(改)

---

### Task 1: 开关 + 密码生成器

**Files:**
- Modify: `services/control-plane/src/control_plane/settings.py`(`keycloak_email_action_lifespan_s` 字段附近)
- Create: `services/control-plane/src/control_plane/member_password.py`
- Test: `services/control-plane/tests/test_member_password.py`

**Interfaces:**
- Produces: `Settings.member_provisioning_mode: Literal["email", "password"]`(默认 `"email"`);`generate_initial_password() -> str`。

- [ ] **Step 1: settings 字段**

在 `keycloak_email_action_lifespan_s` 之后加:

```python
#: 成员/首管开通模式:``email`` = 现状(Keycloak set-password 邮件,依赖
#: SMTP);``password`` = 服务端生成初始密码写进 Keycloak(temporary,
#: 首登强制改密),密码只在创建/重发响应里回传一次。平台级,不分租户。
member_provisioning_mode: Literal["email", "password"] = "email"
```

(`Literal` 已在该文件 import;若无则补。)

- [ ] **Step 2: 写失败测试**

`tests/test_member_password.py`:

```python
import re

from control_plane.member_password import _WORDS, generate_initial_password


def test_format_three_words_dash_four_digits() -> None:
    pw = generate_initial_password()
    m = re.fullmatch(r"([a-z]+)-([a-z]+)-([a-z]+)-(\d{4})", pw)
    assert m is not None
    assert all(w in _WORDS for w in m.groups()[:3])


def test_wordlist_shape() -> None:
    assert len(_WORDS) == 256
    assert len(set(_WORDS)) == 256
    assert all(re.fullmatch(r"[a-z]{4,6}", w) for w in _WORDS)


def test_not_constant() -> None:
    assert len({generate_initial_password() for _ in range(20)}) > 1
```

- [ ] **Step 3: 跑测试确认 import 失败**

Run: `cd services/control-plane && uv run pytest tests/test_member_password.py -q` → FAIL(module 不存在)

- [ ] **Step 4: 实现生成器**

`member_password.py`:

```python
"""成员初始密码生成(password 开通模式,settings.member_provisioning_mode)。

格式 ``word-word-word-NNNN``:3 个词取自内嵌 256 词表(`secrets.choice`,
可重复)+ 4 位数字。~37 bits 熵——这是一次性临时密码(Keycloak
``temporary=True`` 首登强制改密),由管理员人肉转交,强度要求以可读可
转述优先。密码绝不落库、不进审计、不进日志(Global Constraint)。
"""

from __future__ import annotations

import secrets

#: 256 个 4-6 字母小写常用词(可读、可电话转述;无歧义字符要求——整体小写)。
_WORDS: tuple[str, ...] = (
    "acorn", "amber", "anchor", "apple", "april", "arrow", "aspen", "atlas",
    "autumn", "badge", "bagel", "bamboo", "banjo", "basil", "beach", "berry",
    "birch", "bison", "blaze", "bloom", "bluff", "board", "brave", "bread",
    "breeze", "brick", "bridge", "brook", "brush", "bunny", "cabin", "cactus",
    "camel", "candle", "canoe", "canyon", "carrot", "castle", "cedar", "chair",
    "chalk", "cherry", "chess", "chime", "cider", "cliff", "cloud", "clover",
    "cobalt", "cocoa", "comet", "coral", "cotton", "cradle", "crane", "creek",
    "crisp", "crown", "cumin", "daisy", "dance", "dawn", "delta", "denim",
    "desert", "dime", "dove", "draft", "dream", "drift", "dune", "eagle",
    "early", "earth", "ember", "engine", "fable", "falcon", "feather", "fern",
    "field", "flame", "flash", "fleet", "flint", "flora", "flute", "forest",
    "forge", "fossil", "frost", "galaxy", "garden", "garnet", "gecko", "ginger",
    "glacier", "glade", "globe", "gold", "goose", "grain", "grape", "grove",
    "guitar", "gull", "harbor", "hazel", "heron", "hill", "honey", "horizon",
    "hound", "humid", "igloo", "index", "indigo", "iris", "island", "ivory",
    "jade", "jasper", "jelly", "jungle", "juniper", "kayak", "kettle", "kiwi",
    "koala", "lagoon", "lake", "lantern", "larch", "laurel", "lava", "lemon",
    "lily", "linen", "lion", "lotus", "lunar", "lyric", "maple", "marble",
    "meadow", "melon", "mesa", "mint", "mist", "molar", "monsoon", "moon",
    "moss", "motto", "mount", "mural", "myrtle", "nectar", "night", "noble",
    "north", "nova", "oasis", "ocean", "olive", "onyx", "opal", "orange",
    "orbit", "orchid", "otter", "owl", "oxide", "palm", "panda", "paper",
    "peach", "pearl", "pebble", "pecan", "penny", "peony", "pepper", "petal",
    "pine", "pistol", "planet", "plum", "pond", "poplar", "poppy", "prism",
    "quail", "quartz", "quill", "quilt", "rabbit", "rain", "range", "raven",
    "reef", "ridge", "river", "robin", "rocket", "rose", "rowan", "ruby",
    "rustic", "saddle", "sage", "salmon", "sand", "sapphire"[:6], "scarf", "seed",
    "sequoia"[:6], "shade", "shell", "shore", "sierra", "silver", "sketch", "slate",
    "smoke", "snow", "solar", "sonnet", "sparrow"[:6], "spice", "spring", "spruce",
    "stone", "storm", "stream", "summit", "sunny", "swan", "sycamore"[:6], "tango",
    "teal", "tempo", "thistle"[:6], "thunder"[:6], "tiger", "timber", "topaz", "torch",
    "trail", "tulip", "tundra", "turtle", "valley", "velvet", "verse", "vine",
    "violet", "vista", "wagon", "walnut", "watt", "wave", "wheat", "willow",
    "windy", "winter", "wolf", "wren", "yarn", "yellow", "zebra", "zephyr",
)


def generate_initial_password() -> str:
    """``word-word-word-NNNN``;`secrets` RNG,词可重复,数字 0000-9999。"""
    words = "-".join(secrets.choice(_WORDS) for _ in range(3))
    return f"{words}-{secrets.randbelow(10000):04d}"
```

> 实现者注意:上面词表里带 `[:6]` 的字面量是计划书写时截长词的手法——**落码时直接写截断后的最终词**(如 `"sapphi"`、`"sequoi"`、`"sparro"`、`"sycamo"`、`"thistl"`、`"thunde"`),并保证整表恰 256 个、全部 `[a-z]{4,6}`、无重复(test_wordlist_shape 卡死这三条;有重复/超长就换成别的常用词)。

- [ ] **Step 5: 跑测试到绿**

Run: `uv run pytest tests/test_member_password.py -q` → PASS

- [ ] **Step 6: 变异自证**

对 `test_format_three_words_dash_four_digits`:临时把生成器改成 2 词 → red → 还原 → green。对 `test_wordlist_shape`:临时删一个词 → red → 还原 → green。记录在报告里。

- [ ] **Step 7: Commit**

```bash
git add services/control-plane/src/control_plane/settings.py services/control-plane/src/control_plane/member_password.py services/control-plane/tests/test_member_password.py
git commit -m "feat: member_provisioning_mode 开关 + 可读初始密码生成器"
```

---

### Task 2: member 开通链 password 分支(invite / resend)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/member_ops.py`
- Modify: `services/control-plane/src/control_plane/api/members.py`
- Modify: `services/control-plane/src/control_plane/keycloak/fake_admin_client.py`(仅当 reset_password 未记录调用时)
- Test: `services/control-plane/tests/test_members_api.py`(既有文件追加)

**Interfaces:**
- Consumes: Task 1 的 `generate_initial_password`、`Settings.member_provisioning_mode`。
- Produces: `MemberOpResult` 增 `initial_password: str | None = None`;`_provision_keycloak` / `invite_member` / `resend_member` 增关键字参数 `provisioning_mode: str`(传 `settings.member_provisioning_mode`);invite 响应每项与 resend 响应增 `"initial_password"` 键(email 模式恒 `None`)。

- [ ] **Step 1: 写失败测试**(既有 test_members_api.py 的 fixture/客户端模式照抄邻近用例;fake keycloak client 若不记录 reset_password 调用,先给它加 `reset_password_calls: list[tuple[str, str, bool]]` 记录——(user_id, password, temporary))

```python
@pytest.mark.asyncio
async def test_invite_password_mode_sets_temp_password_and_skips_email(app_password_mode, client, fake_keycloak):
    resp = await client.post("/v1/members/invite", json={"invitations": [
        {"email": "pw-mode@example.com", "role": "member"}
    ]})
    assert resp.status_code == 201
    item = resp.json()["data"]["results"][0]
    pw = item["initial_password"]
    assert re.fullmatch(r"[a-z]+-[a-z]+-[a-z]+-\d{4}", pw)
    assert fake_keycloak.reset_password_calls[-1][2] is True          # temporary
    assert fake_keycloak.reset_password_calls[-1][1] == pw            # 响应里的就是写进 KC 的
    assert fake_keycloak.setup_emails_sent == []                      # 不发邮件
    assert fake_keycloak.created_users[-1].email_verified is True     # email_verified


@pytest.mark.asyncio
async def test_invite_email_mode_unchanged(app_email_mode, client, fake_keycloak):
    resp = await client.post("/v1/members/invite", json={"invitations": [
        {"email": "em-mode@example.com", "role": "member"}
    ]})
    assert resp.status_code == 201
    item = resp.json()["data"]["results"][0]
    assert item["initial_password"] is None
    assert fake_keycloak.reset_password_calls == []
    assert len(fake_keycloak.setup_emails_sent) == 1


@pytest.mark.asyncio
async def test_resend_password_mode_regenerates(app_password_mode, client, fake_keycloak):
    # 先邀请拿到 member_id 与第一枚密码,再 resend,断言:新密码 != 旧密码、
    # 又一次 reset_password(temporary=True)、仍然零邮件。
    ...


@pytest.mark.asyncio
async def test_password_never_in_audit(app_password_mode, client, audit_events):
    # invite 后扫全部已 emit 的 audit 事件序列化 JSON,断言初始密码子串不出现。
    ...
```

(`app_password_mode` / `app_email_mode` = 造 app 时 settings 覆盖 `member_provisioning_mode`;具体照该测试文件既有 app fixture 的构造方式,fixture 名可自取。`...` 两条按注释里的断言写全——这是计划对意图的规定,不是留白授权。)

- [ ] **Step 2: 跑测试确认失败**

Run: `uv run pytest tests/test_members_api.py -q -k "password_mode or email_mode_unchanged"` → FAIL

- [ ] **Step 3: 实现**

`member_ops.py`:

```python
@dataclass(frozen=True)
class MemberOpResult:
    member_id: UUID
    status: str
    keycloak_user_id: str | None
    initial_password: str | None = None   # password 模式一次性回传;绝不落库/审计/日志
```

`_provision_keycloak` 增 `provisioning_mode: str` 关键字参数;函数体两处改:

```python
    kc_user = await keycloak.create_user(
        email=email, tenant_id=tenant_id, display_name=display_name,
        email_verified=(provisioning_mode == "password"),
    )
```

尾段(替换原 send_setup_email 块):

```python
    initial_password: str | None = None
    if provisioning_mode == "password":
        # password 模式:临时密码替代 set-password 邮件;temporary=True 首登强制改密。
        # 失败语义与邮件分支一致——不回滚,resend 重驱(届时重新生成)。
        candidate = generate_initial_password()
        try:
            await keycloak.reset_password(user_id=kc_user_id, password=candidate, temporary=True)
            initial_password = candidate
        except KeycloakUnavailableError:
            logger.warning("member.initial_password_failed member_id=%s (resend can retry)", member.id)
    else:
        # Set-password email — failure does not roll back; resend can retry.
        try:
            await keycloak.send_setup_email(user_id=kc_user_id, lifespan_s=email_action_lifespan_s)
        except KeycloakUnavailableError:
            logger.warning("member.setup_email_failed member_id=%s (resend can retry)", member.id)

    return MemberOpResult(
        member_id=member.id, status="invited",
        keycloak_user_id=kc_user_id, initial_password=initial_password,
    )
```

`invite_member` / `resend_member` 增 `provisioning_mode: str` 并透传。`members.py` 两个调用点传 `provisioning_mode=settings.member_provisioning_mode`,invite 的 per-item results 与 resend 响应体各加 `"initial_password": summary.initial_password`。

- [ ] **Step 4: 跑全量成员测试**

Run: `uv run pytest tests/test_members_api.py -q` → 全 PASS(既有用例是 email-mode 回归网)

- [ ] **Step 5: 变异自证**

逐条:①把 `temporary=True` 改 False → red(test_invite_password_mode)→ 还原;②password 分支里补回 send_setup_email 调用 → red(setup_emails_sent 断言)→ 还原;③`email_verified` 恒 False → red → 还原;④audit emit details 里塞 `"pw": candidate` → red(test_password_never_in_audit)→ 还原。全绿后记录。

- [ ] **Step 6: Commit**

```bash
git add -A services/control-plane
git commit -m "feat: 成员邀请/重发 password 开通分支——临时密码替代 SMTP 邮件"
```

---

### Task 3: 首管开通链 password 分支(建租户)

**Files:**
- Modify: `services/control-plane/src/control_plane/api/first_admin.py`
- Modify: `services/control-plane/src/control_plane/api/tenants.py`
- Test: `services/control-plane/tests/test_tenants_api.py`(既有文件追加)

**Interfaces:**
- Consumes: Task 1;与 Task 2 相同的分支形状(独立实现,两文件互不 import 对方的私有函数)。
- Produces: `FirstAdminResult` 增 `initial_password: str | None = None`;`provision_first_admin` 增 `provisioning_mode: str`;`POST /v1/tenants` 响应 `data.first_admin` 增 `"initial_password"`。

- [ ] **Step 1: 写失败测试**(照 test_tenants_api.py 既有 first_admin 用例的 fixture 形状)

```python
@pytest.mark.asyncio
async def test_create_tenant_password_mode_returns_initial_password(app_password_mode, client, fake_keycloak):
    resp = await client.post("/v1/tenants", json={
        "display_name": "PW 租户", "first_admin_email": "boss@example.com",
    })
    assert resp.status_code == 201
    fa = resp.json()["data"]["first_admin"]
    assert re.fullmatch(r"[a-z]+-[a-z]+-[a-z]+-\d{4}", fa["initial_password"])
    assert fake_keycloak.reset_password_calls[-1][2] is True
    assert fake_keycloak.setup_emails_sent == []
    assert fake_keycloak.created_users[-1].email_verified is True


@pytest.mark.asyncio
async def test_create_tenant_email_mode_unchanged(app_email_mode, client, fake_keycloak):
    resp = await client.post("/v1/tenants", json={
        "display_name": "EM 租户", "first_admin_email": "boss2@example.com",
    })
    assert resp.status_code == 201
    assert resp.json()["data"]["first_admin"]["initial_password"] is None
    assert len(fake_keycloak.setup_emails_sent) == 1
```

- [ ] **Step 2: 跑测试确认失败** → FAIL

- [ ] **Step 3: 实现**(镜像 Task 2 的分支形状:`FirstAdminResult.initial_password`、`provision_first_admin(..., provisioning_mode)`、create_user `email_verified=(provisioning_mode == "password")`、密码分支 vs 邮件分支、`tenants.py` 调用点透传 + `first_admin` dict 加键)

- [ ] **Step 4: 跑全量租户测试** → PASS

- [ ] **Step 5: 变异自证**(同 Task 2 的 ①②③ 三刀,打在 first_admin.py 上)

- [ ] **Step 6: Commit**

```bash
git add -A services/control-plane
git commit -m "feat: 建租户首管 password 开通分支——响应一次性回传初始密码"
```

---

### Task 4: 前端一次性凭据面板 + 建租户抽屉

**Files:**
- Create: `apps/admin-ui/src/components/OneTimeCredentialPanel.tsx`
- Modify: `apps/admin-ui/src/components/CreateTenantDrawer.tsx`
- Modify: `apps/admin-ui/src/api/tenants.ts`(first_admin 类型加 `initial_password?: string | null`)
- Modify: `apps/admin-ui/src/i18n/locales/en.ts`、`zh-CN.ts`
- Test: `apps/admin-ui/src/components/__tests__/OneTimeCredentialPanel.test.tsx`、CreateTenantDrawer 既有测试文件追加

**Interfaces:**
- Produces: `<OneTimeCredentialPanel account={email} password={pw} loginUrl={window.location.origin} />`(Task 5 复用);i18n 键 `credential_panel.*`(title / once_warning / account / password / login_url / copy_all / copied)。

- [ ] **Step 1: 写失败测试**

```tsx
// OneTimeCredentialPanel.test.tsx
it("renders account, password, warning and copies all", async () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.assign(navigator, { clipboard: { writeText } });
  render(<OneTimeCredentialPanel account="a@b.com" password="wolf-mint-echo-1234" loginUrl="https://x" />);
  expect(screen.getByText("a@b.com")).toBeInTheDocument();
  expect(screen.getByText("wolf-mint-echo-1234")).toBeInTheDocument();
  expect(screen.getByText(/仅显示这一次|only shown once/i)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: /复制|copy/i }));
  expect(writeText).toHaveBeenCalledWith(expect.stringContaining("wolf-mint-echo-1234"));
  expect(writeText).toHaveBeenCalledWith(expect.stringContaining("a@b.com"));
});
```

CreateTenantDrawer 追加:mock 创建接口返回带 `first_admin.initial_password` → 断言面板出现且密码在文档中;返回 `initial_password: null` → 断言维持原成功文案、无面板。

- [ ] **Step 2: 跑测试失败** → FAIL(组件不存在)

- [ ] **Step 3: 实现**

`OneTimeCredentialPanel`:antd `Alert type="warning"` 包裹(警示文案)+ 三行(登录地址 / 账号 / 初始密码,`Typography.Text copyable` 单项复制)+ 底部「复制全部」按钮(`navigator.clipboard.writeText` 拼三行文本,成功后 `message.success(t('credential_panel.copied'))`)。密码行 `<Typography.Text code>`。**组件不做任何持久化**(无 localStorage/无状态库)。

`CreateTenantDrawer`:创建成功且 `resp.first_admin?.initial_password` 非空 → 抽屉切换到结果视图渲染面板(不立即关抽屉),关闭按钮文案「我已保存,关闭」;`initial_password` 空/缺 → 现行为不变。i18n 双 locale 同步加 `credential_panel` 节(先 grep 两文件确认无同名键)。

- [ ] **Step 4: 跑测试到绿** → PASS;`npx tsc --noEmit` 干净

- [ ] **Step 5: 变异自证**(把面板密码行渲染删掉 → red → 还原;copy_all 只拼 account 不拼 password → red → 还原)

- [ ] **Step 6: Commit**

```bash
git add -A apps/admin-ui
git commit -m "feat(admin-ui): 一次性凭据面板 + 建租户返回初始密码展示"
```

---

### Task 5: 前端成员页(邀请批量结果 + 重发)

**Files:**
- Modify: `apps/admin-ui/src/pages/SettingsMembers.tsx`
- Modify: `apps/admin-ui/src/api/members.ts`(invite 结果项 + resend 响应加 `initial_password?: string | null`)
- Modify: `apps/admin-ui/src/i18n/locales/{en,zh-CN}.ts`(仅本任务新增键,`credential_panel.*` 复用 Task 4 的)
- Test: `apps/admin-ui/src/pages/__tests__/SettingsMembers.test.tsx`(追加)

**Interfaces:**
- Consumes: Task 4 的 `OneTimeCredentialPanel` 与 `credential_panel.*` 键。

- [ ] **Step 1: 写失败测试**:①邀请接口 mock 返回项带 `initial_password` → 邀请弹窗成功后出现凭据面板(多人邀请时每人一块面板,按 email 分组);②`initial_password: null` → 维持现行成功提示、无面板;③重发按钮:mock resend 返回带 `initial_password` → 弹 Modal 标题含「重置密码」且面板在内;返回 null → 维持「邮件已重发」提示。

- [ ] **Step 2: 跑测试失败** → FAIL

- [ ] **Step 3: 实现**:响应驱动(有密码渲染面板,无密码走旧文案),重发按钮文案在响应含密码时下次渲染仍保持原名(按钮名不随 mode 变——前端不知道 mode,只处理响应)。antd Modal 用 `App.useApp()`(仓库既定坑:静态 Modal 测试不渲染)。

- [ ] **Step 4: 跑测试到绿**;`npx tsc --noEmit` + 全量 `npx vitest run` 无回归

- [ ] **Step 5: 变异自证**(重发面板改成不渲染密码 → red → 还原)

- [ ] **Step 6: Commit**

```bash
git add -A apps/admin-ui
git commit -m "feat(admin-ui): 成员邀请/重发一次性初始密码展示"
```

---

### Task 6: 测试集群开关 + runbook

**Files:**
- Modify: `infra/k8s/overlays/test/configmap-patch.yaml`(control-plane env 节加 `EXPERT_WORK_MEMBER_PROVISIONING_MODE: "password"`,照该文件既有键的格式)
- Modify: `docs/runbooks/bootstrap-admin.md`(末尾加一节)

**Interfaces:** 无代码接口;发布后测试集群即 password 模式。

- [ ] **Step 1: overlay 加键**(先读该文件确认 control-plane env 的 patch 形状,照抄相邻键)

- [ ] **Step 2: runbook 节**

```markdown
## 成员开通模式(member_provisioning_mode)

`EXPERT_WORK_MEMBER_PROVISIONING_MODE`:

- `email`(默认):Keycloak set-password 邮件,依赖 realm SMTP 配置。
- `password`:无 SMTP 依赖。建租户(带首管)/邀请成员时服务端生成
  `word-word-word-NNNN` 初始密码写入 Keycloak(temporary,首登强制改密),
  密码只在那一次创建/重发响应与 admin-ui 的一次性面板里出现——不落库、
  不进审计、不进日志,关掉面板后唯一找回方式是「重发」(重新生成)或
  成员页「重置密码」。测试集群当前为 `password`(2026-08-10 起)。
```

- [ ] **Step 3: 本地渲染校验**

Run: `kustomize build infra/k8s/overlays/test | grep MEMBER_PROVISIONING` → 输出该键

- [ ] **Step 4: Commit**

```bash
git add infra/k8s/overlays/test/configmap-patch.yaml docs/runbooks/bootstrap-admin.md
git commit -m "chore: 测试集群开 password 开通模式 + runbook"
```
