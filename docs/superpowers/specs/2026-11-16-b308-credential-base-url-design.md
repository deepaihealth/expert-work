# B-308 平台凭证可填请求地址 —— 设计

- **日期**:2026-11-16 · **状态**:设计稿第 1 版,待用户审。本文只写怎么做,不动代码;开发等用户指令。
- **读者**:产品负责人(看 §0、§9、§10)、开发工程师(全文)、测试(看 §8)。
- **代码事实**:凡写了 `文件:行号` 的,都在 `main` `a0b4349c3` 上打开文件核过。厂商官方接口路径(§4.4 表里「今天没有调用的那几格」)来自厂商文档,本文作者没联网重核,标「开发时再核」。
- **没做的**:没连任何集群、没跑任何付费调用;`packages/expert-work-common/src/expert_work/common/credentials/` 目录对本文作者不可读(工具权限挡住),对它的描述只来自调用方和 `services/control-plane/src/control_plane/tenant_secret_overlay.py:10-15` 的说明。
- **约束**:平台文字(表单说明、报错、日志)一律通用,不提任何租户、不偏向某一家厂商。

---

## 0. 一页看懂

**场景**:系统管理员在「平台凭证」页给某家模型厂商配密钥。有时这把密钥不能直接打官方地址:要走公司自己的转发(出口统一、审计)、要用厂商的另一个地域入口、或者厂商给的是专属部署。今天做不到 —— 每家厂商的地址写死在代码里。

**要做的**:配密钥时多一个可选的「请求地址」。

- 填了:用这把密钥的**所有**调用(对话、看图、向量化、重排、扫描页识别、记忆与辅助调用、质量裁判)都发到这个地址。
- 不填:和今天一样走官方地址。
- 格式:**填到版本号为止**(例如 `https://proxy.example.com/v1`),平台在后面接上这家厂商原本的路径。输入框里灰字显示这家厂商的官方地址,照着改即可。
- 只是「同一家厂商、同一种接口格式、换个入口」。聚合平台(如 OpenRouter)、目录外的模型名、改价格都**不在本项**。

**已拍板(用户 11-16)**:

| # | 内容 |
|---|---|
| 1 | 地址挂在**每一把密钥**上(同一厂商可有多把、按优先级切换),也挂在**按租户的覆盖**上;不挂在整个厂商上 |
| 2 | 对这把密钥的每一种用途都生效 —— 凡是经平台凭证解析拿到这把密钥的地方 |
| 3 | 格式 = 「API 根地址,含版本段」;平台接后面的路径;表单占位显示官方地址 |
| 4 | 保存时校验**并且**每次调用前再校验(防 DNS 重绑定):只许 https;解析后的地址不能是内网 / 本机 / 链路本地 / 云元数据 / 集群内部 |
| 5 | 智能体清单里 `self-hosted` / `azure` 自带的 `base_url` 不在本项,只记为后续风险(§7.2) |

**改动面**:一张迁移(两张表各加一列)、凭证接口加一个字段、一张「厂商 → 官方根地址 + 各用途路径」的表、一个带解析检查的出站连接、12 处构建调用的地方接上地址(§4.6 有清单)、前端表单一个输入框。拆 2 个 PR(§6)。

**和假设不一样的地方**(§2.6 详述,汇报时一并说):

1. ADR-0009 当初定的是「DNS 重绑定不在应用层防,靠网络出口策略」;拍板 4 要在应用层防。本设计按拍板 4 做,并给 ADR-0009 补一节例外(仓库里已有先例:credential-proxy 的出口代理就是先解析、再连检查过的地址)。
2. **向量化今天不看厂商**:不管选哪家,请求都发到通义的地址(`services/orchestrator/src/orchestrator/llm/embedder.py:148`,两处构造都没传地址 `services/control-plane/src/control_plane/runtime.py:1394,1611`)。目录里 OpenAI 和智谱也有向量模型,选了它们会把那家的密钥发给通义。「不填 = 和今天一样走官方」在这两家上今天就是错的。是否顺手修见 §10 问题 2。
3. 通义的重排走的是另一个根地址(`https://dashscope.aliyuncs.com/api/v1/...`,不是 `/compatible-mode/v1`),所以通义的自定义地址必须以 `/compatible-mode/v1` 结尾,平台才能推出重排地址(§4.4)。
4. 按租户的覆盖每个厂商只有一把密钥、没有密钥 ID(`packages/expert-work-persistence/src/expert_work/persistence/models/platform_secret.py:79-80`);「挂在每把密钥上」对租户来说就是挂在那一行覆盖上。

---

## 1. 背景(大白话)

举三个真实会遇到的情形:

- **统一出口**:公司要求所有对外的模型调用都经过自己的转发服务(留痕、限流、出网白名单)。转发服务对外的样子和厂商官方接口一模一样,只是域名不同。
- **地域入口**:同一家厂商在不同地域有不同入口(例如火山方舟 `ark.<地域>.volces.com`),密钥在哪个地域开的就要打哪个入口。
- **专属部署**:厂商给大客户单独部署了一套,接口格式不变,地址是专属的。

这三种情况里,密钥、模型名、接口格式、价格都不变,只有「发到哪」变了。所以本项只做这一件事。

---

## 2. 现状(读代码确认)

### 2.1 密钥怎么存

- 平台密钥一张表,主键是「厂商 + 密钥 ID」,带启用开关和优先级:`packages/expert-work-persistence/src/expert_work/persistence/models/platform_secret.py:27-49`(主键 `:36-39`,优先级 `:42`)。存的永远是 `secret://` / `kms://` 引用,不是明文(`packages/expert-work-protocol/src/expert_work/protocol/platform_secret.py:24-29`)。
- 按租户的覆盖另一张表,主键「租户 + 厂商」,**没有密钥 ID**:`platform_secret.py`(models)`:69-89`。
- 对应的数据对象 `extra="forbid"`,加字段必须同时改这里:`packages/expert-work-protocol/src/expert_work/protocol/platform_secret.py:32-55`(平台)、`:75-96`(租户)。
- 读写仓库:`packages/expert-work-persistence/src/expert_work/persistence/platform_secrets/base.py:41-53`、`:92-102`;SQL 实现 `.../platform_secrets/sql.py:100-139`、`:218-254`(都是「有则更新」的整行覆盖)。
- 最新迁移是 `0190_b300_memory_evidence`(`packages/expert-work-persistence/migrations/versions/0190_b300_memory_evidence.py:27-28`);全仓所有分支都还没有 `0191`(`git log --all` 查过)。本项用 **0191**,开工时再核一次。

### 2.2 接口与页面

- 接口前缀 `/v1/platform/credentials`,只有系统管理员能用:`services/control-plane/src/control_plane/api/platform_config.py:301-306`。
- 写入体 `PlatformSecretWrite`:**每次保存都必须给「粘贴的密钥」或「引用」二选一**(`platform_config.py:69-101`,`:96-101`)。也就是说今天没法「只改优先级不碰密钥」—— 前端切换启用时是把原来的引用原样再发一遍(`apps/admin-ui/src/pages/SettingsPlatformConfig.tsx:280-295`)。
- 写平台密钥:`platform_config.py:388-435`(两个路由 `:437`、`:461`);写租户覆盖:`:761-808`。写完会清缓存、清已构建的智能体并广播给别的副本(`:146-173`;对端处理 `services/control-plane/src/control_plane/invalidation_bus.py:396-401`)。
- 读视图:平台 `:308-386`(每把密钥列出 `key_id / secret_ref / enabled / priority`,`:352-360`);租户 `:692-759`。
- 页面:编辑弹窗 `SettingsPlatformConfig.tsx:965-1096`(密钥 / 引用切换 `:988-1044`,密钥 ID 与优先级 `:1045-1084`);每家厂商的密钥占位字 `:93-98`;租户覆盖抽屉 `:945-958`。前端类型 `apps/admin-ui/src/api/platform_config.ts:16-20`、`:76-81`、`:159-161`。
- 中文文案在 `apps/admin-ui/src/i18n/locales/zh-CN.ts` 的 `settings_platform` 块(`:2775` 起,弹窗相关 `:2816-2841`);`platform_credentials` 只是左侧导航的名字(`:68`)。

### 2.3 环境变量里的密钥

- `platform_provider_credentials: dict[Provider, str]`,只有「厂商 → 引用」,没有地址:`services/control-plane/src/control_plane/settings.py:931-934`;旧的向量化 / 重排引用会补进来(`:956-970`)。
- 合并规则:某厂商只要数据库里有行,就以数据库为准、环境变量不再用(`services/control-plane/src/control_plane/platform_secrets.py:160-174`)。

### 2.4 密钥怎么解析到调用方

- `PlatformSecretsService` 把环境变量和数据库合并,缓存 30 秒(`platform_secrets.py:51`),对外只给**引用字符串**:单把最优(`:66-73`)、按优先级的列表(`:75-84`)、租户视图(`:86-101`、`:108-123`)。租户覆盖是「一把密钥替换整个列表」(`:96-100`)。
- 解析器是一个子类 `TenantOverlayCredentialsResolver`(`services/control-plane/src/control_plane/tenant_secret_overlay.py:33-83`),父类在 `expert-work-common/credentials`,那个目录**禁止改**(`tenant_secret_overlay.py:10-15`)。接线在 `services/control-plane/src/control_plane/app.py:1563-1592`。
- 拿到引用之后,调用方各自去读密钥值、各自拼地址。分两类:

**A. 走统一的模型路由 `build_llm_router`**(`services/orchestrator/src/orchestrator/agent_factory.py:2732-2870`)。它对每一把引用读出密钥、调 `_build_provider` 建客户端(`:2832-2840`),地址由厂商决定(`:3224-3419`)。调用方有两种给引用的方式:
  - 传 `provider_key_resolver`,拿多把密钥做切换(`:2819`;绑定租户的实现 `services/control-plane/src/control_plane/runtime.py:758-789`):智能体主模型、看图(`agent_factory.py:809-855`)、升级模型(`:760-768`)、分步路由(`:2902-2974`)、长期记忆写回(`services/control-plane/src/control_plane/memory/writeback_processor.py:452-469`)。
  - 调用方自己先解析出一把引用,塞进 `ModelSpec.api_key_ref` 再交给路由(`agent_factory.py:2799-2802`):裁判(`runtime.py:916-924`)、质量裁判(`services/control-plane/src/control_plane/quality_judge.py:126`、`:140`)、辅助模型(`services/control-plane/src/control_plane/aux_model_adapter.py:113`、`:130-133`、`:153`)、重排的聊天模型分支(`runtime.py:1518-1527`、`:1728`)。

**B. 不走路由,自己发 HTTP**:
  - 向量化:`runtime.py:1384-1397`、`:1603-1611` → `HTTPEmbeddingClient`,地址默认通义(`embedder.py:33,37,148`)。
  - 厂商重排接口:`runtime.py:1497-1510`、`:1702-1707` → `services/control-plane/src/control_plane/rerank_vendors.py:105-133` → 通义原生重排(`services/orchestrator/src/orchestrator/llm/rerank.py:48-49,69-73`)或智谱重排(`services/orchestrator/src/orchestrator/llm/rerank_cohere.py:113-119,206-227`)。
  - 知识库扫描页识别与图片描述:密钥由 `ResolvingProviderKeys` 给(`runtime.py:1401-1414`,接线 `app.py:2232-2236`),地址查两张写死的表(`services/control-plane/src/control_plane/knowledge/recognition/clients.py:40-53`),用在 `recognizer.py:329-340`、`figure_pipeline.py:111-117`。

### 2.5 写死的地址与路径(全部)

| 位置 | 今天的值 |
|---|---|
| `services/orchestrator/src/orchestrator/llm/providers/anthropic.py:87`,拼接 `:343`、`:406` | `https://api.anthropic.com` + `/v1/messages` |
| `.../providers/openai.py:89-90`,拼接 `:305`、`:360` | `https://api.openai.com` + `/v1/chat/completions` |
| `.../providers/openai_compatible.py:92` | Kimi `https://api.moonshot.cn` + `/v1/chat/completions` |
| `openai_compatible.py:94-95` | 智谱 `https://open.bigmodel.cn` + `/api/paas/v4/chat/completions` |
| `openai_compatible.py:108` | DeepSeek `https://api.deepseek.com` + `/v1/chat/completions` |
| `openai_compatible.py:110` | 通义 `https://dashscope.aliyuncs.com/compatible-mode` + `/v1/chat/completions` |
| `openai_compatible.py:112-113` | 豆包 `https://ark.cn-beijing.volces.com` + `/api/v3/chat/completions` |
| `embedder.py:33,37` | 向量化 `https://dashscope.aliyuncs.com/compatible-mode` + `/v1/embeddings`(**所有厂商**) |
| `rerank.py:48-49` | 通义重排 `https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank` |
| `rerank_cohere.py:113-115` | 智谱重排 `https://open.bigmodel.cn` + `/api/paas/v4/rerank` |
| `clients.py:40-53` | 智谱版面解析 `.../api/paas/v4/layout_parsing`;六家的识别用聊天地址(与上面一致) |

各厂商的工厂函数本来就收 `base_url` 参数(`openai_compatible.py:121-228`),但构建处从不传(`agent_factory.py:3372`)。

### 2.6 其它相关事实

- **按厂商类型、不按地址判断的东西**(换地址不受影响,已逐条核过):智谱的「账户状态类 429」按回包错误码判(`openai_compatible.py:103-106`,用在 `rerank_cohere.py:117,280`、`clients.py:215`);智谱流式工具参数按厂商名开(`agent_factory.py:3370`);DeepSeek 回传思考按厂商名开(`:3380`);思考档位、输出上限、温度按目录里的「厂商 + 模型名」查(`:3306-3341`);豆包的模型名(包括接入点 ID)原样发出,与地址无关。代码里没有任何地方按域名判断厂商行为(搜过 `bigmodel / volces / moonshot / deepseek.com` 等,只出现在常量和注释里)。
- **限流桶、重排节奏按「密钥引用」分**,不看地址:`agent_factory.py:2849-2853`;`services/orchestrator/src/orchestrator/llm/rerank_guard.py:290-293`。同一把密钥换入口,厂商那边的额度还是同一份,所以不用改。
- **熔断标识**已经把地址算进去了,但传的是清单里的 `entry.base_url`(只有 self-hosted / azure 才有):`agent_factory.py:2862`、`services/orchestrator/src/orchestrator/llm/failover.py:252-263`。
- **进程级共享 HTTP 客户端**:`app.py:1377-1388`,没设 `follow_redirects`(httpx 默认不跟随跳转),`trust_env` 用默认。
- **出站网络**:`infra/k8s` 里没有任何 `NetworkPolicy`(搜过)。有安全组的只有沙箱和 credential-proxy;沙箱那份说明写明不加注解时 pod 落在集群安全组、「和平台在一起」,能直连数据库、Redis、k8s API(`infra/k8s/sandbox-test/security-group-patch.yaml:1-12`)。控制面 pod 的安全组规则不在仓库里,本文无法确认它们对内网的出站是否被拦。自定义的公网 https 地址不会被仓库里的任何配置挡住。
- **现有的地址校验**:`packages/expert-work-common/src/expert_work/common/url_validation.py`。静态检查(不解析域名)`:116-143`;拦的网段 `:42-56` **不含 `100.64.0.0/10`**,阿里云元数据地址 `100.100.100.200` 在这个段里,所以现有静态检查放得过它(只记录,见 §7.3)。另有「解析后逐个检查、连检查过的那个地址」的函数 `:146-190`,credential-proxy 的出口代理在用(`services/credential-proxy/src/credential_proxy/egress_proxy.py:44,61`)。
- **ADR-0009**:`docs/adr/0009-control-plane-egress-ssrf-dns-rebind.md:12-14` 定「DNS 重绑定不在应用层防,靠出口网络策略」,`:66-79` 把出口策略列为部署前提。拍板 4 要求每次调用前再校验,与它不同,§4.5.3 说明怎么处理。
- **离线评测脚本不经平台凭证**:`tools/eval/*` 从环境变量读密钥和地址(例如 `tools/eval/kb_agent_eval.py:157-158`),不受本项影响。

---

## 3. 业界做法(简)

本地 `~/src/github/` 没有专门再查;常见做法(OpenAI / Anthropic 官方 SDK、LangChain、LiteLLM、Dify、One-API 等)一致是「每个凭证 / 每个模型供应商配置一个 base URL,填到版本段为止,SDK 在后面接 `/chat/completions` 之类的路径」。本设计的格式与此一致,管理员从这些工具里抄来的地址可以直接用。本文不引具体文件,开发时如需引用再核。

---

## 4. 设计

### 4.1 数据

迁移 **0191_b308_credential_base_url**:

- `platform_provider_secret` 加 `base_url TEXT NULL`;
- `tenant_provider_secret` 加 `base_url TEXT NULL`;
- 两列都加检查约束 `base_url IS NULL OR base_url LIKE 'https://%'`(兜底,真正的校验在接口层);
- 不回填,`NULL` = 官方地址,所以旧数据行为不变;降级删列。

数据对象 `PlatformProviderSecretRecord` / `TenantProviderSecretRecord` 各加 `base_url: str | None = None`;仓库的两个 `upsert_*` 加同名参数,照写入值覆盖。

**环境变量里的密钥不带地址**。理由:环境变量是一个「厂商 → 引用」的简单映射(`settings.py:931-934`),要带地址就得改成嵌套结构并改启动校验;而要用自定义地址时,在页面上给这家厂商加一行数据库密钥即可(加了之后这家厂商就以数据库为准,`platform_secrets.py:160-174`)。

### 4.2 接口

`PlatformSecretWrite`(`platform_config.py:69-101`)加:

- `base_url: str | None`,**区分「没传」和「传了空」**:没传 = 保持这行原来的地址;传 `null` 或空字符串 = 清掉(回到官方)。用 pydantic 的 `model_fields_set` 判断。这样今天的「切换启用」请求(只带原引用,`SettingsPlatformConfig.tsx:283-287`)不会顺手把地址清掉。
- 「只改地址、不换密钥」:沿用今天的做法,前端把原引用随请求带上(引用不是密钥,本来就回显在页面上)。接口的「二选一」规则不变。

保存时的检查(§4.5.1)全过才写库。新错误码(都是 422,只有冲突是 409):

| 码 | 什么时候 |
|---|---|
| `BASE_URL_NOT_SUPPORTED` | 厂商是 `azure` / `self-hosted`(它们的地址来自清单,不在本项) |
| `BASE_URL_INVALID` | 不是 https、带用户名密码、带 `?` 或 `#`、没有主机名、超过 2048 字符 |
| `BASE_URL_HAS_PATH_SUFFIX` | 末尾已经带了平台要接的路径(如 `/chat/completions`)—— 提示「填到版本号为止」 |
| `BASE_URL_QWEN_ROOT` | 通义的地址不以 `/compatible-mode/v1` 结尾(§4.4) |
| `BASE_URL_BLOCKED_HOST` | 主机名或解析出的任一地址落在禁止范围(§4.5.1) |
| `BASE_URL_UNRESOLVABLE` | 域名解析失败 |
| `BASE_URL_CONFLICT`(409) | 同一厂商下,另一行(平台或任一租户)用着**同一个引用**但地址不同(§4.3 解释为什么要拦) |

读视图:每把密钥、每条租户覆盖都多返回 `base_url`;每个厂商多返回 `official_base_url`(§4.4 表的第二列),前端拿它当占位字,不在前端另抄一份。

审计:写入的 `details` 加 `base_url`(规范化后的,或 `null`)。地址是配置不是密钥;用户名密码和查询串在入口就被拒了(见下 §4.7 关于路径里藏令牌的处理)。

### 4.3 解析顺序与「地址跟着引用走」

**顺序**:先按今天的规则选出这次用哪把密钥(有租户覆盖用覆盖,否则平台的多把按优先级),**地址取自选中的那一行**;那一行没填地址就用官方地址。

- 租户覆盖不填地址 → 官方地址,**不继承**平台那几把密钥的地址(覆盖本来就是换一把自己的密钥,`platform_secrets.py:96-100`)。这一点请用户确认(§10 问题 1)。
- 平台多把密钥:各用各的地址,切换时连地址一起切。例:主密钥走转发、备用密钥走官方,转发挂了就切到官方。

**怎么把地址送到调用的地方**:解析器父类不能改(`tenant_secret_overlay.py:10-15`),今天 12 处调用方拿到的都只是引用字符串(§2.4)。改返回类型要动全部调用方和父类,所以采用「按 (厂商, 引用) 查地址」:

- `PlatformSecretsService` 在每次重载缓存时(`platform_secrets.py:148-199`)顺手建一张 `(厂商, 引用) → 地址` 的表(平台行和所有租户行都进),新方法 `base_url_for(provider, secret_ref) -> str | None`。
- 编排层定义一个函数类型 `EndpointLookup = Callable[[str, str], Awaitable[str | None]]`,由控制面把上面的方法传进去。编排层不依赖控制面。
- 这张表要求「同一厂商下同一引用只对应一个地址」,否则查出来的不是选中那一行的地址。粘贴密钥生成的引用每行都不同(平台按密钥 ID、租户按租户 ID,`platform_config.py:247-276`),只有管理员手填的外部引用可能撞;所以保存时拦下「同引用不同地址」(`BASE_URL_CONFLICT`)。
- 时效:写入的那台副本立即生效(清缓存 + 清已构建的智能体,`platform_config.py:421-422`);其它副本靠广播(`invalidation_bus.py:396-401`),最迟 30 秒缓存到期(`platform_secrets.py:51`)。和今天换密钥一样。

### 4.4 地址怎么拼(每家厂商)

在编排层新加一张表(建议放 `orchestrator/llm/providers/endpoints.py`):每家厂商一个**官方根地址**(含版本段),每种用途一个**后缀**。官方地址 = 官方根地址 + 后缀;自定义地址 = 管理员填的根地址(去掉末尾 `/`)+ 同一个后缀。旧的常量(`KIMI_BASE_URL` 等)保留并继续导出,别处还在引用(例如 `tools/eval/kb_eval.py:150`)。

| 厂商 | 表单占位(官方根地址) | 对话 | 向量化 | 重排 | 扫描页版面解析 |
|---|---|---|---|---|---|
| anthropic | `https://api.anthropic.com/v1` | `/messages` | — | — | — |
| openai | `https://api.openai.com/v1` | `/chat/completions` | `/embeddings`(开发时再核) | — | — |
| kimi | `https://api.moonshot.cn/v1` | `/chat/completions` | — | — | — |
| glm | `https://open.bigmodel.cn/api/paas/v4` | `/chat/completions` | `/embeddings`(开发时再核) | `/rerank` | `/layout_parsing` |
| deepseek | `https://api.deepseek.com/v1` | `/chat/completions` | — | — | — |
| qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `/chat/completions` | `/embeddings` | 见下 | — |
| doubao | `https://ark.cn-beijing.volces.com/api/v3` | `/chat/completions` | — | — | — |
| azure / self-hosted | 表单不显示这一项 | — | — | — | — |

扫描页识别的「看图转文字」用的是同一家的对话地址(`clients.py:46-53`)。

**通义的重排**:原生重排接口不在 `/compatible-mode/v1` 下面(`rerank.py:48-49`)。规则:通义的根地址必须以 `/compatible-mode/v1` 结尾;重排地址 = 去掉这段后接 `/api/v1/services/rerank/text-rerank/text-rerank`。官方情况下拼出来正好是今天的地址。不以这段结尾的通义地址保存时就拒(`BASE_URL_QWEN_ROOT`),而不是让重排悄悄走官方 —— 拍板 2 要求每种用途都跟着走。这条限制是否可以接受见 §10 问题 4。

**不会出现双 `/v1`**:根地址含版本段,后缀不含;保存时拒绝末尾已带后缀的地址(`BASE_URL_HAS_PATH_SUFFIX`)。末尾多一个 `/` 自动去掉。

**拼接必须和今天逐字节一致**:对每家厂商、每种今天在用的用途,「官方根地址 + 后缀」要等于 §2.5 表里今天的地址。这条写成测试(§7.1 第 1 条)。

### 4.5 校验

#### 4.5.1 保存时(控制面接口)

1. 厂商不是 `azure` / `self-hosted`。
2. 解析 URL:协议只许 `https`;不许用户名密码、`?`、`#`;要有主机名;端口可有;长度 ≤ 2048。
3. 路径末尾不能已经是这家厂商任一用途的后缀;通义要以 `/compatible-mode/v1` 结尾。
4. 主机名本身:拒绝 `localhost` 等本机名、单段名字(没有点)、以 `.local` / `.internal` / `.svc` / `.cluster.local` 结尾的名字、非标准写法的 IP(复用 `url_validation.py:59-76` 的全角点折叠与 `:98-106` 的写法检查)。
5. 解析域名(或直接是 IP),**每一个**地址都必须是公网地址,判定见下。解析失败也拒。
6. 同一厂商下同一引用不能有两个不同地址(§4.3)。

**「公网地址」怎么判**(拍板 4 的范围落到网段):先把 IPv4 映射地址(`::ffff:a.b.c.d`)、NAT64(`64:ff9b::/96`)、6to4(`2002::/16`)里嵌着的 IPv4 拆出来再判;然后满足任一即拒:`is_global` 为假、组播、未指定,或落在显式名单 `0.0.0.0/8`、`10.0.0.0/8`、`100.64.0.0/10`(含阿里云元数据 `100.100.100.200` 与内网 DNS)、`127.0.0.0/8`、`169.254.0.0/16`、`172.16.0.0/12`、`192.168.0.0/16`、`198.18.0.0/15`、`::1`、`fc00::/7`、`fe80::/10`。显式名单和 `is_global` 两道都查,因为不同 Python 版本对 `is_global` 的判定有差异(本地 3.14 实测:`224.0.0.1` 的 `is_global` 为真、`64:ff9b::a00:1` 为真,所以组播和 NAT64 必须单独处理);测试要在 CI 的 Python 版本上跑。集群的服务网段、pod 网段都在私网段里,被上面覆盖。

这套判定新写在 `expert-work-common` 一个新函数里(例如 `url_validation.py` 加 `check_public_endpoint`),**不改**现有 `validate_remote_url` 的行为 —— 那个函数给 MCP、webhook 等用,改它的范围不在本项(§7.3)。

#### 4.5.2 每次调用前(防 DNS 重绑定)

只给「用了自定义地址」的调用另建一个进程级 HTTP 客户端(和 `app.py:1377` 的共享客户端并列),它的连接层在**建立每一条新 TCP 连接时**:解析域名 → 所有地址按 §4.5.1 第 5 条判 → 有一个不合格就整条拒 → 合格则**连这个检查过的地址**。TLS 证书仍按原域名校验(连接层只换了「连哪个 IP」,握手时的服务器名还是原域名)。这样「检查时是公网、连接时变内网」的窗口被关上,不只是「调用前查一下」。

- 实现提示:httpcore 1.0.9 的连接池接受自定义的网络后端(`connect_tcp` 那一步),httpx 0.28.1 的默认传输层没把这个参数露出来,需要写一个小的传输层包装;PR 1 先做一个小验证再展开(版本见 `uv.lock`)。
- 这个客户端 `trust_env=False`(不吃环境里的代理变量,否则检查的是代理地址)、不跟随跳转。
- 已经建好、在复用的长连接不再重复解析 —— 它连的就是当初检查过的地址。
- 被拒时抛「连接失败」类错误:路由按今天的规则切到同厂商的下一把密钥(`agent_factory.py:2825-2864`),全部失败才报错给用户;报给用户的是通用的「模型服务地址不可用」,不带地址。
- 官方地址的调用**完全不走**这个客户端,行为、性能与今天相同。

#### 4.5.3 与 ADR-0009 的关系

ADR-0009 说应用层的「解析后钉住地址」脆弱、不做。本项按拍板 4 做,理由:① 这里的目标地址是管理员手填、会长期存在的配置,出错后果是平台密钥连同对话内容发到内网服务;② 仓库里已有同样做法的先例(`egress_proxy.py:44,61` 用 `url_validation.py:146-190`);③ 只影响自定义地址这一条路,范围小、可测。PR 1 给 ADR-0009 加一节「例外:平台凭证的自定义请求地址」,写明范围和实现方式;出口网络策略仍是部署前提,两层都要。

### 4.6 要接上地址的调用处(12 处)

| # | 位置 | 怎么接 |
|---|---|---|
| 1 | 模型路由 `build_llm_router`(`agent_factory.py:2732`) | 新参数 `endpoint_lookup`,**必填、无默认值**(测试与离线脚本显式传 `None`),这样漏传是类型错误而不是悄悄走官方;每把引用查一次地址,传给 `_build_provider` |
| 2 | `_build_provider`(`agent_factory.py:3224`) | 新参数 `base_url`;anthropic / openai / 五家兼容厂商按 §4.4 拼;self-hosted / azure 忽略它、照旧用清单地址 |
| 3 | 熔断标识(`agent_factory.py:2862`) | 传「实际用的地址」(自定义地址或清单地址),换地址后熔断状态重新计 |
| 4 | 智能体构建 / 看图 / 升级 / 分步路由(`agent_factory.py:745,768,830,2916-2974`) | 与 `provider_key_resolver` 同路径透传 |
| 5 | 记忆写回(`writeback_processor.py:452`) | 同上 |
| 6 | 裁判(`runtime.py:918`)、质量裁判(`quality_judge.py:140`)、辅助模型(`aux_model_adapter.py:153`,委派策略也走它) | 传 `endpoint_lookup` |
| 7 | 重排的聊天模型分支(`runtime.py:1523`、`:1728`) | 同上 |
| 8 | 向量化(`runtime.py:1393-1397`、`:1611`) | 查地址,拼 `/embeddings`(依赖 §10 问题 2 的答复决定是否同时改成按厂商取官方地址) |
| 9 | 厂商重排(`rerank_vendors.py:105-133` → `rerank.py:69`、`rerank_cohere.py:206`) | `vendor_reranker` 加 `base_url` 参数,通义按 §4.4 推重排地址,智谱接 `/rerank` |
| 10 | 扫描页识别与图片描述(`ResolvingProviderKeys`,`runtime.py:1401-1414`) | 返回「密钥 + 根地址」;`recognizer.py:329-340`、`figure_pipeline.py:111-117` 改为按根地址拼,不再查写死的表 |
| 11 | 用了自定义地址的客户端都换用 §4.5.2 的客户端 | 在上面各处选客户端时统一判断 |
| 12 | 编排层的地址表(§4.4) | 新文件;旧常量保留 |

限流桶、重排节奏、密钥值缓存都按引用分,**不改**(§2.6)。

### 4.7 能看到「在用自定义地址」(不泄露地址里的东西)

- **页面**:密钥表加一列「请求地址」,显示「官方」或自定义地址的主机名;租户覆盖表同样一列。
- **指标**:
  - `expert_work_platform_credentials_custom_endpoints`(数值):当前填了地址的行数(平台 + 租户),在缓存重载时设置,与已有的 `expert_work_platform_credentials_tenant_overrides`(`platform_secrets.py:37-40`、`:198`)同一处。
  - `expert_work_custom_endpoint_connections_total{provider, outcome}`:§4.5.2 的连接层每建一条新连接计一次,`outcome` ∈ `ok` / `blocked` / `unresolvable` / `connect_error`。只有自定义地址经过它,官方调用不计。
- **日志**:被拒时一条 `provider_endpoint.blocked provider=… host=… reason=…`。**只记主机名,不记路径**:有的转发服务把令牌放在路径里(例如 `/<令牌>/v1`),路径一律不进日志、指标和链路追踪。
- **链路**:向量化、重排已有的 span(`runtime.py:1390-1392` 一类)加属性 `endpoint=custom`;不加地址本身。
- **审计**:见 §4.2,记规范化后的完整地址(管理员可见的配置);表单说明写明「地址里不要放密钥」。

### 4.8 页面

编辑 / 新增密钥弹窗、租户覆盖弹窗都加一项(放在「启用」之前):

- 标签:**请求地址(可选)**
- 占位:这家厂商的官方根地址(来自接口的 `official_base_url`)
- 说明:「不填就用官方地址。填写时只填到版本号为止,例如 `https://你的地址/v1`,平台会在后面接上具体接口路径。只支持 https;地址里不要放密钥。用这把密钥的所有调用(对话、看图、向量化、重排、文档识别等)都会发到这里。」
- `azure` / `self-hosted` 不显示这一项。
- 编辑已有密钥时,「粘贴密钥」改为可空:空着 = 不换密钥(前端带上原引用);新增时仍必填。
- 「切换启用」请求不带 `base_url`,按 §4.2 保持原地址。
- 中英文案进 `settings_platform` 块(`zh-CN.ts:2775` 起,英文同名块)。
- 后端的错误码映射成人话提示(例如 `BASE_URL_HAS_PATH_SUFFIX` →「只填到版本号为止,不要带 /chat/completions」)。

---

## 5. 不做什么

- 聚合平台(OpenRouter 等)、目录外的模型名、价格随地址变化 —— 用户 11-16 明确收窄,不做。
- 环境变量里的密钥带地址(§4.1)。
- 按地址单独限流:转发服务自己的限流由它自己报 429,平台按今天的 429 处理。
- 保存时真打一次厂商接口测连通(§10 问题 3,默认不做)。
- 智能体清单里的 `self-hosted` / `azure` `base_url`(§7.2,只记风险)。
- 修 `validate_remote_url` 漏拦 `100.64.0.0/10`(§7.3,只记录)。
- 离线评测脚本(`tools/eval/*`)不经平台凭证,不改。

---

## 6. PR 拆分

| PR | 内容 | 依赖 |
|---|---|---|
| **PR 1 后端** | 迁移 0191;数据对象 / 仓库 / 接口字段与校验;`PlatformSecretsService.base_url_for` 与冲突检查;编排层地址表;带解析检查的连接层与独立客户端;§4.6 全部 12 处;熔断标识;指标与日志;ADR-0009 补节;单元测试(§7.1)。是否带上向量化按厂商取地址的修复,看 §10 问题 2 | 无 |
| **PR 2 前端 + 手册** | 弹窗字段、占位、页面列、可空的「粘贴密钥」、切换启用保留地址、中英文案、错误提示;前端测试;测试手册新增一节(§8) | PR 1 |

两个 PR 可同一班发测试。没有总开关:不填地址的行为与今天相同(向量化那一处除外,见 §10 问题 2)。

---

## 7. 风险与测试

### 7.1 测试(单元,带「改坏了会红」的变异点)

1. **官方地址逐字节不变**:每家厂商 × 今天在用的每种用途,新表拼出的官方地址 == §2.5 表的旧值。变异:改任一后缀或根地址 → 红。
2. **自定义拼接**:`https://p.example.com/v1/` + 对话 → `https://p.example.com/v1/chat/completions`;通义 `https://p.example.com/x/compatible-mode/v1` 的重排 → `https://p.example.com/x/api/v1/services/rerank/text-rerank/text-rerank`。变异:不去末尾 `/`、通义不剥段 → 红。
3. **保存校验表**:`http://…`、`https://u:p@h/v1`、带 `?a=1`、带 `#x`、末尾 `/chat/completions`、通义不以 `/compatible-mode/v1` 结尾、`azure` 带地址;IP:`127.0.0.1`、`10.1.2.3`、`172.16.41.73`、`192.168.0.1`、`169.254.169.254`、`100.100.100.200`、`198.18.0.1`、`224.0.0.1`、`::1`、`fd00::1`、`fe80::1`、`::ffff:10.0.0.1`、`64:ff9b::a00:1`、`2002:a00:1::`;名字:`localhost`、`api`(单段)、`x.svc`、`x.svc.cluster.local`、`x.internal`、全角点写的 IP;解析结果「一个公网一个内网」→ 全拒;正常公网域名 → 过。每一行删掉对应的判断都应变红(逐条变异)。
4. **重绑定**:解析桩第一次给公网、第二次给内网;保存通过,调用时新连接被拒且计 `blocked`。再断言连接层拿到的是检查过的 IP、TLS 用的服务器名是原域名。变异:连接层改回「按域名连」→ 红。
5. **没传 = 保持,传空 = 清掉**:先存地址,再发一个不带 `base_url` 的保存(模拟切换启用)→ 地址还在;发 `null` → 清掉。变异:把「没传」当 `null` → 红。前端同样测「切换启用」请求不带该字段。
6. **冲突**:两行同厂商同引用不同地址 → 409;同地址 → 过。
7. **多把密钥各用各的**:主密钥带自定义地址、备用不带;构建出的两个句柄地址不同、熔断标识不同;主的连接被拒时切到备用。
8. **每个调用处都接上**:§4.6 的 #4~#10 各一个测试,断言请求打到自定义地址。变异:任一处不查地址 → 该测试红。`endpoint_lookup` 必填 → 漏传是类型错误。
9. **租户覆盖**:覆盖带地址 → 用它;覆盖不带 → 官方(即使平台密钥带地址)。按 §10 问题 1 的答复定断言。
10. **日志不漏路径**:地址路径里放一个标记串,跑一遍被拒和成功的调用,所有日志记录、指标标签里都没有这个串。
11. **只改地址也会让已构建的智能体失效**:保存后 `_invalidate_built_agents` 被调用、广播事件发出。

### 7.2 记录的风险:清单里的 `self-hosted` / `azure` 地址(不在本项)

逐处查过(在 `packages/`、`services/control-plane/src`、`services/orchestrator/src`、`services/credential-proxy/src` 的非测试代码里搜 `base_url`,共 17 个文件;除下面几处外,都是厂商客户端自己的地址字段,或 searxng / keycloak / 沙箱 / 工作区 / 内部 mTLS 等无关用途):

- 字段定义在 `packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py:141-146`,协议层没有任何校验器引用它。
- 控制面源码里没有一处读 `ModelSpec.base_url`(`services/control-plane/src` 里出现 `base_url` 的 5 个文件 `app.py / keycloak/admin_client.py / keycloak/token.py / runtime.py / settings.py` 都是别的用途)。
- 编排层只检查「有没有填」:`agent_factory.py:3384-3385`、`:3398-3402`;然后直接用(`:3387-3389`、`:3404-3411`)。不限协议、不查内网。
- 用的密钥是平台给这个厂商配的密钥(`agent_factory.py:2819`、`:2833`)。
- 控制台没有这个字段的表单,只能在清单原文里写(`apps/admin-ui/src/components/manifest-editor/groups/ModelRoutingSection.tsx:34-36`)。

所以:**只要平台配了 `self-hosted` 或 `azure` 的密钥**,任何能改清单的人都能让平台带着这把密钥、连同对话内容,发到他指定的任意地址(含 http 和内网地址)。没配这两家密钥的部署不受影响;测试 / 生产是否配了,本文无法从仓库确认(`infra/k8s` 里没有相关配置)。建议另开一项:把 §4.5 的同一套校验用到清单地址上,或者把这两家的地址也收到平台凭证上。

### 7.3 记录的风险:现有静态检查漏 `100.64.0.0/10`

`url_validation.py:42-56` 不拦 `100.64.0.0/10`,阿里云元数据 `100.100.100.200` 在里面。MCP 注册、webhook 等用这个函数(`services/control-plane/src/control_plane/api/mcp_servers.py:518`、`api/webhook_endpoints.py:124` 等)。本项新写的判定会拦它,但不改旧函数。建议另开一项补上。

### 7.4 其它风险

- **转发服务看得到密钥和全部内容**:这是管理员的选择;表单说明里写清,审计记谁改的、改成什么。
- **转发服务行为不同**:例如把流式回包攒着一起发,会触发首 token 超时;或者吞掉某些请求头。只能靠测试环境验收(§8)发现。
- **漏接一处**:靠 `endpoint_lookup` 必填 + §7.1 第 8 条逐处测试兜住。
- **每条新连接多一次解析**:有长连接复用,影响小;解析慢会算进首 token 时间。
- **本地开发**:有的本地代理把公网域名解析到 `198.18.0.0/15`,会被拦;本地测试用桩,不受影响。
- **Python 版本差异**:`ipaddress` 的判定随版本变(§4.5.1),测试必须在 CI 的版本上跑。

---

## 8. 测试环境验收(写进测试手册新的一节)

**前置**(开发完成后由执行人准备,本文不代做):

- 一个**公网可解析、带正规证书**的 https 反向代理地址,把 `/<前缀>/...` 原样转到某一家厂商官方接口。建议选智谱(对话、向量化、重排、版面解析都能覆盖到):`https://<代理域名>/glm/api/paas/v4` → `https://open.bigmodel.cn/api/paas/v4`。代理开访问日志,能按路径数请求。
- 测试环境有一个用智谱的智能体、一个绑了知识库的智能体;平台向量化 / 重排若不是智谱,本节只验对话与识别。
- 费用:十来次小调用,预计不到 ¥1。

| 编号 | 做什么 | 判据 |
|---|---|---|
| A1 | 给智谱的某把密钥填上代理地址,保存 | 保存成功;页面「请求地址」列显示代理主机名;审计里有这次修改 |
| A2 | 用这把密钥的智能体问一句话 | 正常答复;代理日志出现 `/chat/completions`;指标 `…_connections_total{outcome="ok"}` 增加 |
| A3 | 看图、知识库检索(带重排)、上传一页扫描件 | 代理日志依次出现对话、`/embeddings`(若向量化是智谱)、`/rerank`、`/layout_parsing` |
| A4 | 依次保存这些地址:`http://<代理域名>/glm/api/paas/v4`、`https://127.0.0.1/v1`、`https://100.100.100.200/v1`、一个解析到 `10.x` 的公网域名、`https://<代理域名>/glm/api/paas/v4/chat/completions` | 全部被拒,提示分别对应「只支持 https」「地址不允许」「只填到版本号为止」 |
| A5 | 同厂商再加一把备用密钥(不填地址、优先级更低),停掉代理,再问一句 | 仍正常答复(切到备用、走官方);代理恢复后再问,回到代理 |
| A6 | 清空地址 | 再问一句,代理日志没有新请求 |
| A7 | 在控制面日志里搜代理路径前缀 `/glm/` | 搜不到(只有主机名) |
| A8(可选,需要能改 DNS) | 保存一个自己控制的域名,保存后把它改解析到内网地址,等缓存过期再问 | 新连接被拒、计 `blocked`、切到备用密钥 |

---

## 9. 通用性自检

- 表单、报错、日志文字不提任何租户、不偏向哪家厂商;各厂商的差异只在 §4.4 那张表里,按厂商类型而不是按某个客户定。
- 校验规则对所有厂商相同,只有通义多一条「以 `/compatible-mode/v1` 结尾」,原因是它的重排接口结构,不是业务原因。

---

## 10. 要用户定的(只列真正没定的)

1. **租户覆盖不填地址时**:走官方地址(本设计的做法),还是继承平台那几把密钥的地址?本设计选前者,因为覆盖本来就是「换一把这个租户自己的密钥」。
2. **向量化今天不看厂商**(§0 第 2 条):选 OpenAI / 智谱的向量模型时,请求带着那家的密钥发到通义。本项要按厂商拼地址,顺手修成「按厂商取官方地址」最自然,但这会改变「不填地址」时的行为(从错的变成对的)。放进 B-308 PR 1,还是另开一项?(建议放进 PR 1,并在发测试前用只读查询确认测试 / 生产的平台向量化选的是哪家。)
3. **保存时要不要真打一次厂商接口**验证地址可用(会花一次极小的调用,且需要知道该用哪个模型名)?本设计默认不打,只做格式、解析和内网检查,问题留给第一次真实调用暴露。
4. **通义的地址必须以 `/compatible-mode/v1` 结尾**才能推出重排地址;如果某个转发服务给通义换了别的路径结构,就配不上。可以接受吗?不接受的话,需要给通义多一个「原生接口地址」输入框。
