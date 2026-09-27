# Stream OFFICE — 企业办公能力包（国内）

> 给 per-user 持久 agent 补足"日常办公占 70%"的工具面。本轮 = 文件处理能力（读写
> Excel/Word/PPT/PDF + 数据分析 + 中文）+ 办公 Skill；**国内协作连接器（钉钉/企微/
> 飞书）本轮暂缓**（受 MCP-client-only 方向 + 国内 remote MCP 生态约束）。

## 0. 来源与定位
- 触发：Stream TE 引擎层完成后，引擎能"承载"办公能力；用户拍板**服务国内客户、去国外连接器**（Gmail/Slack/Drive 不做）。
- 与 [[project_mcp_direction_client_only]] 一致：消费外部 MCP，不自造 server。
- 与 [[project_target_product_form]] 一致：per-user 持久 agent 的办公场景。

## 0.1 现状（已 file:line 核实，2026-06-05）
- 沙箱镜像 `infra/sandbox-image/Dockerfile` = `python:3.12-alpine`（~50MB）+ 纯 stdlib，**运行时卸载 pip**（安全 F-2/F-13）；无办公库、无中文字体、无 locale。
  > **2026-09-25 更正**：「运行时卸载 pip」已不成立——B-81 之后沙箱配了 pip 内网镜像源（注入点 `exec_envs`），测试环境真实路径复验 `pip download pymupdf` 11.31s 成功（此前四次 `pip install` 全部超时失败）。详见 `docs/superpowers/specs/2026-09-25-platform-office-skills-design.md` §0.1。
- 镜像名 supervisor **硬编码** `settings.sandbox_image`（`settings.py:34`，env `EXPERT_WORK_SANDBOX_SANDBOX_IMAGE`）；`AcquireRequest`（`schemas.py:16`）无 image 字段；一个 supervisor 实例当前无法按请求选镜像。
- `SandboxSpec`（`agent_spec.py:225`）**已有未使用的 `image` / `image_build` 字段**；无 `image_variant`。
- `persistent_workspace`（J.15）是 manifest→supervisor 传递的**完美模板**：manifest → `agent_factory.build_tool_registry` → 沙箱工具字段 → `run_in_sandbox` → `acquire`。
- MCP catalog（Stream W）**只收 remote**（`sse`/`streamable_http`，`mcp_connector_catalog.py:37`），不收 stdio；钉钉/企微/飞书要进 catalog 必须有 HTTP MCP endpoint。
- Skill（Stream X）`SKILL.md` 打包就绪 + 中文 prompt-injection 扫描（`test_threat_patterns_chinese.py`）。

## 0.2 已锁决策（用户拍板，2026-06-05）
- **文件能力** = 纯 Python 库起步（slim + build-time pip）；libreoffice/pandoc 重型转换**推后**（512MB 内存撑不住 + gVisor 兼容风险）。
- **镜像** = 独立 office variant（minimal 保纯净给计算 agent；office 给办公），manifest `image_variant` 选。
- **国内连接器** = 本轮**暂缓**，记 backlog。

## 1. Mini-ADRs

### OFFICE-ADR-1 文件能力 = 纯 Python 库（slim 基础镜像）
- **决策**：office 镜像基于 `python:3.12-slim`（glibc → pandas/numpy/Pillow 有 manylinux wheels 直接装；alpine musl 需现编译）；`docker build` 阶段 `pip install` 办公库，**装完卸载 pip**（保运行时安全：仍无 pip、无 egress）。
- **库集**：`pandas` / `openpyxl`（xlsx）/ `python-docx`（docx）/ `python-pptx`（pptx）/ `pypdf` + `pdfplumber`（PDF 读）/ `Pillow`（图像）/ `matplotlib`（图表，中文需字体）。
- **不含**（推后）：libreoffice（.doc/.xls 老格式转换、Office→PDF 渲染）、pandoc、wkhtmltopdf。真有"格式互转"需求再开 OFFICE-1c。
- **2026-09-25 更正（pip）**：`装完卸载 pip` 已不成立——见 §0.1 本节更正注（B-81 之后运行时 pip 可用）。
- **2026-09-25 更正（libreoffice）**：「不含（推后）：libreoffice」已不成立——镜像早已装有 `libreoffice-writer-nogui` / `-calc-nogui` / `-impress-nogui`（自 #763 起），B-55 收官时 pod 内复探仍确认 `soffice` 二进制在位。详见 `docs/superpowers/specs/2026-09-25-platform-office-skills-design.md` §0.1。

### OFFICE-ADR-2 镜像 variant 机制（复用 persistent_workspace 链路）
- **决策**：`SandboxSpec` 加 `image_variant: Literal["minimal","office"] | None`（None→默认/minimal，向后兼容）。链路照搬 `persistent_workspace`：
  - `agent_factory` 读 `spec.spec.sandbox.image_variant` → `build_tool_registry(image_variant=...)` → 各沙箱工具（exec_python/bash/read_file/write_file/edit_file/list_dir）字段 → `run_in_sandbox(image_variant=...)` → `SupervisorClient.acquire(image_variant=...)`。
  - supervisor：`AcquireRequest` 加 `image_variant`；settings 加 `sandbox_image_office`；`_select_image(variant)` 选镜像名（未知/None → 默认 minimal）。
- **诚实约束**：variant 是 **agent/sandbox 级**（一个 agent 的所有沙箱工具同镜像），不是 per-tool；从 manifest 一处定。

### OFFICE-ADR-3 中文支持
- **决策**：office 镜像装 Noto CJK / 思源黑体 + `fontconfig` + 设 `LANG=zh_CN.UTF-8`（兼 UTF-8/GB18030 读写）。matplotlib 中文渲染配字体。验收：沙箱内生成含中文的 xlsx/docx/pptx/图表不乱码。
- **实现要点（OFFICE-1b 落地实证）**：① Debian `fonts-noto-cjk` 装的是 `.ttc` 集合，fontconfig 能看到 SC/TC/JP 全部子面，但 **matplotlib 只注册第 0 面**（家族名 `Noto Sans CJK JP`）；该面属统一 Noto Sans CJK,含全部简体汉字,渲染中文不豆腐。② build 期 `mpl_cjk_rc.py` 把 `font.sans-serif: Noto Sans CJK JP, …` 烤进 matplotlib **系统级 matplotlibrc**(只读 rootfs 下也生效,因 /workspace tmpfs 每会话清空,用户级 rc 不持久),agent 画图开箱即用,无需自配字体;只写 `key: value`(注释行会触发 rc parser 告警污染 stderr)。③ build 期 `fc-cache -f` 预热字体缓存进只读层。

### OFFICE-ADR-4 国内连接器暂缓（不破 client-only）
- **决策**：钉钉/企微/飞书连接器**本轮不做**。国内官方 remote MCP server 生态薄，catalog 只收 remote；强上要么破 client-only 自搭 wrapper、要么仅单租户 on-prem stdio off-catalog。记 backlog，待生态成熟或单租户场景再开。Expert Work 本就 backend-only、不内置末端 adapter。

### OFFICE-ADR-5 办公 Skill = 平台导入端点（不自写，2026-06-05 改定）
- **背景（实证后改向）**：调研 ClawHub / anthropics/skills，办公 skill 的"现成轮子"没有能无脑直接搬的——Anthropic 官方 docx/xlsx/pptx/pdf 是 **Proprietary 明文禁移植**；ClawHub 第三方多**无 LICENSE**（版权全保留）+ 质量/安全参差。**故 Expert Work 不自写、也不批量移植办公 skill**。
- **决策**：补一个**平台级 skill 导入端点** `POST /v1/platform/skills/import`（system_admin + `bypass_rls`，multipart `.skill` ZIP，复用租户侧 `_skill_zip` 解析 + 威胁扫描），让平台管理员把 `.skill` 包导入成**平台级 skill**（NULL-tenant），租户经 X-6 merged view + X-4 resolver 自动可见 + `required_tier` 门控。**包来源由管理员定**（自打 / 挑 license 干净的现成包），平台不预置内容。
- **content_hash 幂等（租户+平台统一）**：导入同名 skill 时，与 **latest 版本 content_hash 相同则跳过**（不产生冗余版本），不同则 `add_version` 生成新版本。租户现有 `POST /v1/skills/import` **每次都加版本**，本轮一并补幂等，两边语义一致。
- store 接口齐全无需新方法：`get_skill_by_name`/`get_platform_skill_by_name` + `get_version_by_number`/`get_platform_version_by_number`（取 latest content_hash）+ `add_version`/`add_platform_version`/`create_*`。
- **2026-09-25 修订**：推翻本条「包来源由管理员定（自打 / 挑 license 干净的现成包），平台不预置内容」一句——`docx`/`pptx`/`xlsx`/`pdf` 四个技能改为**平台自写**（clean-room），源码放本仓库 `platform-skills/`，走 PR + CI。理由：① 这四个技能目前是 2026-08-03 从 `anthropics/skills` 原样导入的第 1 版，许可证明文禁止衍生作品与再分发，在其上修改不可行；② 原版「新建」路线依赖 npm（docx→npm `docx` 包、pptx→`pptxgenjs`），而沙箱里没有 npm（B-55 已删），模型读了技能也照做不了。旧版本（Anthropic 原版）保留在版本历史里不删，许可风险用户知情选择。详见 `docs/superpowers/specs/2026-09-25-platform-office-skills-design.md`。

## 2. Stream 切分
- **OFFICE-1a 镜像 variant 机制**：manifest `image_variant` + supervisor `_select_image` + acquire 字段 + orchestrator 链路。先用现有 minimal 镜像验证机制（不依赖 office 镜像就绪）。
- **OFFICE-1b office 镜像** ✅：`infra/sandbox-image-office/Dockerfile`（slim + 库 + 中文）+ CI 构建（`.github/workflows/sandbox-image-office.yml`，阿里云 ACR）+ supervisor settings（1a 已接）。**runner.py 不复制**：build context = `infra/`，COPY 共享 `sandbox-image/runner.py`（防安全敏感文件 drift）。smoke（`smoke_test.py` 驱动 runner 协议 + `smoke_payload.py` 内验 import + 中文渲染）本地实跑通过；gVisor/read-only-rootfs 验证留目标主机。
- **OFFICE-3 平台 skill 导入端点** ✅（PR #437）：`POST /v1/platform/skills/import`（system_admin + bypass_rls，multipart `.skill` ZIP，复用 `parse_skill_zip` + moderation + U-21 strict scan）+ 租户/平台 **content_hash 幂等**（同 latest hash 跳过返 `200`+`created:false`，否则 `add_version` 返 `201`+`created:true`）。顺带补租户 `POST /v1/skills/import` 此前缺的幂等。**不自写/不批量移植 skill 内容**（OFFICE-ADR-5）。纯 API，无 migration。
- **OFFICE-2 国内连接器**：暂缓（backlog）。

依赖：`OFFICE-1a → OFFICE-1b`（机制先于镜像，但 1a 用 minimal 可独立验）。OFFICE-3 与镜像无依赖，可独立先行。

## 3. CI / 约束
- **manifest schema 改动**（SandboxSpec 加 image_variant）走 protocol 包；无 DB migration（image_variant 是 runtime，不入库）。
- office 镜像 build：`python:3.12-slim` + pip install 后卸 pip；CI 加构建步骤（现状 CI 不构建 sandbox 镜像，需新增 office 镜像 build/tag）。
- 内存：纯 Python 库内存可控（不像 libreoffice）；大文件处理仍受 512MB 限，文档注明上限。
- gVisor：纯 Python 库（C 扩展 pandas/numpy/Pillow）在 gVisor 的兼容性需 integration 验（比 libreoffice 风险低）。
- 每 PR 零技术债 + 同步 ITERATION-PLAN（[[feedback_iteration_plan_sync_after_ship]]）；push 前 preflight。

## 4. Verification
- **OFFICE-1a**：manifest `image_variant: office` → acquire 收到 variant → supervisor `_select_image` 选对镜像名；None/未知 → 默认 minimal（向后兼容，现有 agent 不变）。
- **OFFICE-1b**：office 镜像内 `import pandas/openpyxl/docx/pptx/pypdf/pdfplumber/PIL` 成功；生成含中文的 xlsx/docx/pptx + matplotlib 图表不乱码；运行时无 pip。
- **OFFICE-3**：平台管理员 `POST /v1/platform/skills/import` 上传 `.skill` ZIP → 平台 skill；租户经 merged view 可见；同名导入 content_hash 同则跳过、不同则加版本（租户+平台一致）；非 system_admin 403；威胁扫描拦恶意包。

**完成 = 国内办公场景文件处理能力生产级 + 办公 Skill 可用**（连接器待生态）。
