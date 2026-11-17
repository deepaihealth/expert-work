# B-308 平台凭证可填请求地址 —— 设计

- **日期**:2026-11-16(第 10 版·简单版 2026-11-17)· **状态**:**第 10 版·简单版**,用户 11-17 定的最终范围:系统管理员可信,只防手误;**改地址必须同时给新密钥**。存储结构回到 `main` 的做法(密钥仍在原来的密钥库、固定槽位、就地覆盖),凭证表只加两列。第 6~9 版与半个第 10 版的版本化槽位、对账回收、外部引用扫描、行内加密等全部撤掉,原因与清单见 §11。PR 1 后端已在分支上按本版实现。
- **读者**:产品负责人(看 §0、§7.4、§9)、开发工程师(全文)、测试(看 §8)、后续审查(**先看 §0 的威胁模型,按它定级**)。
- **代码事实**:凡写了 `文件:行号` 的,`main` 的事实在 `main` `a0b4349c3` 上核过;httpx / httpcore 的行号在本机虚拟环境 httpx 0.28.1 / httpcore 1.0.9(与 `uv.lock` 一致)里核过。厂商官方接口路径里「今天平台没有调用的那几格」来自厂商文档,本文作者没联网重核,标「开发时再核」。
- **没做的**:没连任何集群、没跑任何付费调用;`packages/expert-work-common/src/expert_work/common/credentials/` 目录对本文作者不可读(工具权限挡住),对它的描述只来自调用方和 `services/control-plane/src/control_plane/tenant_secret_overlay.py:10-15` 的说明;`docs/runbooks/credentials.md` 同样读不到(没有绕开),见 §4.1 末。
- **约束**:平台文字(表单说明、报错、日志)一律通用,不提任何租户、不偏向某一家厂商。

---

## 0. 一页看懂

**场景**:系统管理员在「平台凭证」页给某家模型厂商配密钥。有时这把密钥不能直接打官方地址:要走公司自己的转发(出口统一、审计)、要用厂商的另一个地域入口、或者厂商给的是专属部署。今天做不到 —— 每家厂商的地址写死在代码里。

**要做的**:配密钥时多一个可选的「请求地址」。

- 填了:用这把密钥的**所有**调用(对话、看图、向量化、重排、扫描页识别、记忆与辅助调用、质量裁判、在集群里跑的评测脚本)都发到这个地址。
- 不填:和今天一样走官方地址。
- 格式:**填到版本号为止**(例如 `https://proxy.example.com/v1`),平台在后面接上这家厂商原本的路径。输入框里灰字显示这家厂商的官方地址,照着改即可。
- **只接受公网 https 地址**。内网、本机、云元数据、集群内部地址一律不行,没有「内网白名单」—— 转发服务需要有公网 https 入口。
- **改地址必须同时给新密钥**:这次保存让地址变了(主机、端口、路径任一不同),就必须同时粘贴密钥原文、或换成另一个引用;否则拒绝,提示「换地址请同时填写这个服务的密钥」。清空回官方不用。
- 只是「同一家厂商、同一种接口格式、换个入口」。聚合平台(如 OpenRouter)、目录外的模型名、改价格都**不在本项**。
- **要知道的一点**:自定义地址连不上、超时或返回 5xx 时,平台**不会**改用同一家厂商的另一把密钥(哪怕那把走官方地址),而是直接切到智能体配置的备用模型,没有备用模型就报错;限流、欠费、密钥失效这三类仍和今天一样切到同厂商的下一把密钥;另有两种情况**连备用模型也不会切**、这一轮直接报错:转发服务返回「请求格式类」的 4xx(例如路径配错得到 400 / 404,`router.py:559-565`),以及已经开始输出之后流中断(`router.py:544-548`)(现有路由规则,§2.6;本项不改,另立 B-309)。

### 0.1 威胁模型(后续审查按这一节定级)

**前提**:系统管理员是少数几个可信的内部人员。凭证页只有系统管理员能用(`platform_config.py:301-306`)。

**要防的(手误与外部变化)**:

| # | 情形 | 靠什么防 |
|---|---|---|
| T1 | 地址填错:不是 https、带了 `/chat/completions`、缺版本段、通义没写 `/compatible-mode/v1` | 保存时的格式检查(§4.5.1),拒绝并给出这家厂商的正确示例 |
| T2 | 填了内网 / 本机 / 云元数据 / 集群内部地址(含用域名指过去) | 保存时解析后逐个检查、只许公网(§4.5.1) |
| T3 | **改成转发服务的地址却忘了换密钥**,于是把公司在厂商那里的真密钥发给了第三方(Langfuse 社区 #15401 同类问题,§3) | 「改地址必须同时给新密钥」(§4.2);密钥库先写、凭证行后写(§4.2 写入顺序);带自定义地址的密钥不进值缓存(§4.3) |
| T4 | 地址填得对,但服务连不上 / 密钥被拒 | 保存前真打一次的连通测试(§4.5.3),可勾「仍然保存」 |
| T5 | 转发服务的域名事后被解析到内网(对方改了 DNS) | 每条新连接都重新解析并检查(§4.5.2) |
| T6 | 地址里的路径(有的转发服务把令牌放在路径里)进日志或审计 | `httpx` 日志过滤、审计只记主机与路径指纹(§4.7) |

**不防的(写进 §5;后续审查发现的问题如果只在这些情形下才出现,最高定 LOW,不作为阻塞)**:

- 恶意的系统管理员:有意新建一行、填一个指向公司真密钥的引用再配上转发地址;利用两个人同时保存的先后、利用缓存窗口、利用滚动发布或回滚期间新旧镜像并存的边角,把密钥送到别处。系统管理员本来就能看到引用、能改智能体和平台配置,这些不在本项的防护范围。
- 能直接改数据库、改集群配置、读密钥库的人。
- 保存过程中数据库或密钥库出错留下的半截状态(§4.2 写入顺序里写明了后果,管理员看到报错重存即可)。
- 消息广播丢失时的短暂不一致,除非它会让 T3 在**管理员操作完全正确**时发生(那一种由 §4.3 的「不进值缓存」堵上)。

**已拍板**:

| # | 内容 |
|---|---|
| 1 | 地址挂在**每一把密钥**上(同一厂商可有多把、按优先级切换),也挂在**按租户的覆盖**上;不挂在整个厂商上(11-16) |
| 2 | 对这把密钥的每一种用途都生效 —— 凡是经平台凭证拿到这把密钥的地方(11-16) |
| 3 | 格式 = 「API 根地址,含版本段」;平台接后面的路径;表单占位显示官方地址(11-16) |
| 4 | 保存时校验**并且**每次新连接前再校验(防 DNS 重绑定):只许 https;解析后的地址不能是内网 / 本机 / 链路本地 / 云元数据 / 集群内部(11-16) |
| 5 | 智能体清单里 `self-hosted` / `azure` 自带的 `base_url` 不在本项,只记为后续风险(§7.2)(11-16) |
| 6 | 租户覆盖不填地址 → 官方地址,不继承平台那几把密钥的地址(11-16) |
| 7 | 「向量化不看厂商」的问题在 B-308 里修,作为 PR 1 里**单独一个提交**、单独一条验收(§4.9)(11-16) |
| 8 | 填了或改了地址时,保存前**真打一次**:最多 15 秒;失败默认拒绝并说明原因;管理员可勾「仍然保存」(地址不允许除外);这次调用照常记用量(§4.5.3)(11-16) |
| 9 | 通义的地址必须以 `/compatible-mode/v1` 结尾,否则拒绝并在提示里给出正确格式(11-16) |
| 10 | **改地址必须同时给新密钥**(粘贴原文,或换成另一个引用);清空回官方免填(11-17,取代 11-16 的「必须当场粘贴、同主机例外」) |
| 11 | 内网 / 内部地址不开白名单,只许公网 https(11-16) |
| 12 | 路由的切换规则本次不改,单独立项(B-309)(11-16) |
| 13 | 简单版:系统管理员可信,只防手误;密钥存储不改结构(固定槽位、就地覆盖);不做行内加密、版本化槽位、回收、比较后写(11-17) |

**改动面**:一张迁移(两张凭证表各加两列)、凭证写入体加两个字段(`base_url`、`force_save`)、一张「厂商 → 官方根地址 + 各用途路径」的表、凭证解析器直接返回「引用 + 地址」、一个只给自定义地址用的出站客户端(解析后只连检查过的地址)、保存时的连通测试、`httpx` 日志去掉路径、前端表单。拆 2 个 PR(§6),向量化的修复在 PR 1 里单独一个提交。

**和最初假设不一样的地方**(§2.6 详述):

1. ADR-0009 当初定的是「DNS 重绑定不在应用层防,靠网络出口策略」;拍板 4 要在应用层防。本设计按拍板做,并给 ADR-0009 补一节例外(仓库里已有先例:credential-proxy 的出口代理就是先解析、再连检查过的地址)。
2. **向量化今天不看厂商**:不管选哪家,请求都发到通义的地址(`services/orchestrator/src/orchestrator/llm/embedder.py:148`,两处构造都没传地址 `services/control-plane/src/control_plane/runtime.py:1394,1611`)。目录里 OpenAI 和智谱也有向量模型,选了它们会把那家的密钥发给通义。拍板 7 定在本项里修。
3. 通义的重排走的是另一个根地址(`https://dashscope.aliyuncs.com/api/v1/...`,不是 `/compatible-mode/v1`),所以有拍板 9。
4. 按租户的覆盖每个厂商只有一把密钥、没有密钥 ID(`packages/expert-work-persistence/src/expert_work/persistence/models/platform_secret.py:79-80`)。
5. 集群里跑的两份评测脚本**确实用平台凭证**(`tools/eval/b300_inpod.py:603-628`、`tools/eval/compaction_retention.py:374-381`),它们在本项范围内(§4.6)。
6. `httpx` 每次请求都会在 INFO 级别打出完整地址(httpx 0.28.1 `_client.py:1741`),而平台根日志是 INFO、没有调高 `httpx` 的级别(`packages/expert-work-common/src/expert_work/common/observability/log.py:217-224`,`services/control-plane/src/control_plane/settings.py:45`)。控制面进程一导入 langfuse,langfuse 就把 `httpx` 日志器调到 WARNING(`langfuse/logger.py:21-22`),这行请求日志本来就不打;评测脚本、命令行等不导入 langfuse 的进程才会打。过滤器照做(纵深防御),由单元测试咬住,验收写法见 A10。

---

## 1. 背景(大白话)

举三个真实会遇到的情形:

- **统一出口**:公司要求所有对外的模型调用都经过自己的转发服务(留痕、限流、出网白名单)。转发服务对外的样子和厂商官方接口一模一样,只是域名不同。这个转发服务要有公网 https 入口,平台不连内网地址。
- **地域入口**:同一家厂商在不同地域有不同入口(例如火山方舟 `ark.<地域>.volces.com`),密钥在哪个地域开的就要打哪个入口。
- **专属部署**:厂商给大客户单独部署了一套,接口格式不变,地址是专属的。

这三种情况里,密钥、模型名、接口格式、价格都不变,只有「发到哪」变了。所以本项只做这一件事。

---

## 2. 现状(读代码确认)

### 2.1 密钥怎么存

- 平台密钥一张表,主键是「厂商 + 密钥 ID」,带启用开关和优先级:`packages/expert-work-persistence/src/expert_work/persistence/models/platform_secret.py:27-49`(主键 `:36-39`,优先级 `:42`)。存的永远是 `secret://` / `kms://` 引用,不是明文(`packages/expert-work-protocol/src/expert_work/protocol/platform_secret.py:24-29`)。
- 按租户的覆盖另一张表,主键「租户 + 厂商」,**没有密钥 ID**:`platform_secret.py`(models)`:69-89`。
- 数据对象 `extra="forbid"`,加字段必须同时改:`packages/expert-work-protocol/src/expert_work/protocol/platform_secret.py:32-55`(平台)、`:75-96`(租户)。
- 读写仓库:`packages/expert-work-persistence/src/expert_work/persistence/platform_secrets/base.py:41-53`、`:92-102`;SQL 实现 `.../platform_secrets/sql.py:100-139`、`:218-254`(整行覆盖)。
- 最新迁移是 `0190_b300_memory_evidence`(`packages/expert-work-persistence/migrations/versions/0190_b300_memory_evidence.py:27-28`),对应测试 `packages/expert-work-persistence/tests/test_migration_0190_b300_memory_evidence.py`;全仓所有分支都还没有 `0191`。本项用 **0191**,开工时再核一次。

### 2.2 接口与页面

- 接口前缀 `/v1/platform/credentials`,只有系统管理员能用:`services/control-plane/src/control_plane/api/platform_config.py:301-306`。
- 写入体 `PlatformSecretWrite`:每次保存都必须给「粘贴的密钥」或「引用」二选一(`platform_config.py:69-101`,`:96-101`);**工具密钥的写入也用同一个写入体**(`:486-519`、`:810-853`)。前端切换启用时是把原来的引用原样再发一遍(`apps/admin-ui/src/pages/SettingsPlatformConfig.tsx:280-295`)。
- 写平台密钥:`platform_config.py:388-435`(路由 `:437`、`:461`);写租户覆盖:`:761-808`。写完会清缓存、清已构建的智能体并广播给别的副本(`:146-173`;对端处理 `services/control-plane/src/control_plane/invalidation_bus.py:396-401`)。审计今天记完整引用(`:423-434`)。
- 读视图:平台 `:308-386`(每把密钥 `key_id / secret_ref / enabled / priority`,`:352-360`;被几个智能体引用 `:361`;平台自身用途 `:364`);租户 `:692-759`。
- 页面:编辑弹窗 `SettingsPlatformConfig.tsx:965-1096`(密钥 / 引用切换 `:988-1044`,密钥 ID 与优先级 `:1045-1084`);每家厂商的密钥占位字 `:93-98`;租户覆盖抽屉 `:945-958`。前端类型 `apps/admin-ui/src/api/platform_config.ts:16-20`、`:76-81`、`:159-161`。
- 中文文案在 `apps/admin-ui/src/i18n/locales/zh-CN.ts` 的 `settings_platform` 块(`:2775` 起,弹窗相关 `:2816-2841`);`platform_credentials` 只是左侧导航名(`:68`)。

### 2.3 环境变量里的密钥

- `platform_provider_credentials: dict[Provider, str]`,只有「厂商 → 引用」,没有地址:`services/control-plane/src/control_plane/settings.py:931-934`;旧的向量化 / 重排引用会补进来(`:956-970`)。
- 某厂商只要数据库里有行,就以数据库为准、环境变量不再用(`services/control-plane/src/control_plane/platform_secrets.py:160-174`)。

### 2.4 密钥怎么解析到调用方

- `PlatformSecretsService` 合并环境变量和数据库,缓存 30 秒(`platform_secrets.py:51`),对外只给**引用字符串**:单把最优(`:66-73`)、按优先级的列表(`:75-84`)、租户视图(`:86-101`、`:108-123`)。租户覆盖是「一把密钥替换整个列表」(`:96-100`)。
- 解析器是控制面的子类 `TenantOverlayCredentialsResolver`(`services/control-plane/src/control_plane/tenant_secret_overlay.py:33-83`),父类在 `expert-work-common/credentials`,那个目录**禁止改**(`:10-15`)。接线 `services/control-plane/src/control_plane/app.py:1563-1592`;评测脚本自己再建一个(`tools/eval/b300_inpod.py:604-613`)。
- 拿到引用之后,各调用方自己读密钥值、自己拼地址。

**A. 走统一的模型路由 `build_llm_router`**(`services/orchestrator/src/orchestrator/agent_factory.py:2732-2870`):对每一把引用读出密钥、调 `_build_provider` 建客户端(`:2832-2840`),地址由厂商决定(`:3224-3419`)。引用有两种来法:
  - 传 `provider_key_resolver`(类型 `agent_factory.py:2325`,返回引用列表;控制面实现 `runtime.py:758-789`):智能体主模型、看图(`agent_factory.py:809-855`)、升级模型(`:760-768`)、分步路由(`:2902-2974`);控制面里构建智能体的入口 `runtime.py:1287`,子智能体 `services/control-plane/src/control_plane/subagent_runtime.py:625`、`:852`;长期记忆写回 `services/control-plane/src/control_plane/memory/writeback_processor.py:452-469`;评测脚本 `tools/eval/compaction_retention.py:374-381`(自己从服务取引用列表)。
  - 调用方先解析出**一把**引用,塞进 `ModelSpec.api_key_ref` 再交给路由(`agent_factory.py:2799-2802`):
    - 裁判 `_build_judge_caller`(`runtime.py:886`、`:916-924`),经 `resolve_defenses`(`:1010`)从三处进入:主构建 `runtime.py:1272`、子智能体 `subagent_runtime.py:610`、`:837`;
    - 质量裁判 `services/control-plane/src/control_plane/quality_judge.py:126-145`;
    - 辅助模型 `services/control-plane/src/control_plane/aux_model_adapter.py:113-155`,被这些地方建出来:委派策略 `services/control-plane/src/control_plane/delegation_policy.py:184`、知识库画像 `app.py:2297`、记忆整理 `app.py:2438`、技能演化 `app.py:2515`、评测脚本 `tools/eval/b300_inpod.py:628`;
    - 重排的聊天模型分支 `runtime.py:1518-1527`、`:1728`。

**B. 不走路由,自己发 HTTP**:
  - 向量化:`runtime.py:1384-1397`、`:1603-1611` → `HTTPEmbeddingClient`,地址默认通义(`embedder.py:33,37,148`)。
  - 厂商重排:`runtime.py:1497-1510`、`:1702-1707` → `services/control-plane/src/control_plane/rerank_vendors.py:105-133` → 通义原生重排(`services/orchestrator/src/orchestrator/llm/rerank.py:48-49,69-73`)或智谱重排(`services/orchestrator/src/orchestrator/llm/rerank_cohere.py:113-119,206-227`)。
  - 扫描页识别与图片描述:密钥由 `ResolvingProviderKeys` 给(`runtime.py:1401-1414`,接线 `app.py:2232-2236`),地址查写死的表(`services/control-plane/src/control_plane/knowledge/recognition/clients.py:40-53`),用在 `recognizer.py:329-340`、`figure_pipeline.py:111-117`;发请求时没传共享客户端就自己新建一个(`clients.py:174-183`)。

**HTTP 客户端从哪来**:厂商客户端都经 `client_for`(`services/orchestrator/src/orchestrator/llm/providers/_http.py:27-60`)取客户端:给了共享的就用共享的,否则**每次新建一个普通客户端**(`:60`)。质量裁判、辅助模型调路由时不传共享客户端(`quality_judge.py:140-142`、`aux_model_adapter.py:153-155`),评测脚本自己建一个(`b300_inpod.py:615`)。所以「在某一层传一个特殊客户端」管不住所有路径。

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

- **按厂商类型、不按地址判断的东西**(换地址不受影响,逐条核过):智谱「账户状态类 429」按回包错误码判(`openai_compatible.py:103-106`,用在 `rerank_cohere.py:117,280`、`clients.py:215`);智谱流式工具参数按厂商名开(`agent_factory.py:3370`);DeepSeek 回传思考按厂商名开(`:3380`);思考档位、输出上限、温度按目录的「厂商 + 模型名」查(`:3306-3341`);豆包的模型名(含接入点 ID)原样发出。代码里没有按域名判断厂商行为的地方(搜过 `bigmodel / volces / moonshot / deepseek.com`,只出现在常量和注释里)。
- **限流桶、重排节奏按「密钥引用」分**,不看地址:`agent_factory.py:2849-2853`;`services/orchestrator/src/orchestrator/llm/rerank_guard.py:290-293`。同一把密钥换入口,厂商那边额度不变,不用改。
- **熔断标识**已把地址算进去,但传的是清单里的 `entry.base_url`:`agent_factory.py:2862`、`services/orchestrator/src/orchestrator/llm/failover.py:252-263`。
- **出错后路由怎么走**:厂商客户端把 `httpx.HTTPError` 转成 `LLMNetworkError`(`openai.py:313-314`;anthropic 同理 `anthropic.py:355`)。路由把错误分两种(`services/orchestrator/src/orchestrator/llm/router.py:501-508`):**密钥级**(限流、欠费、密钥失效、这把的熔断打开,`router.py:210-215`)→ 试同厂商同模型的下一把密钥;**厂商级**(5xx、网络错误、超时)→ **跳过同一组里剩下的所有密钥**,直接去下一个厂商 / 模型(`router.py:566-575`,`services/orchestrator/src/orchestrator/llm/failover.py:311-317`),理由是「5xx / 网络问题对每把密钥都一样」。有了每把密钥各自的地址,这个理由不再总成立;用户 11-16 定本项不改这条规则,另立 B-309,后果见 §4.3。
- **进程级共享客户端**:`app.py:1377-1414`,不设默认超时、`keepalive` 64 条、55 秒过期、不限总连接数,并且禁用 cookie(防跨租户回放,`:1414` 起的说明)。
- **日志**:`init_logging` 只给根日志设级别(`log.py:217-224`),默认 INFO(`settings.py:45`,调用处 `app.py:1345-1349`);`httpx` 每次请求打 `HTTP Request: <方法> <完整地址> …`(httpx 0.28.1 `_client.py:1741`)。
- **出站网络**:`infra/k8s` 里没有 `NetworkPolicy`。有安全组的只有沙箱和 credential-proxy;沙箱那份说明写明不加注解时 pod 落在集群安全组、「和平台在一起」,能直连数据库、Redis、k8s API(`infra/k8s/sandbox-test/security-group-patch.yaml:1-12`)。控制面 pod 的安全组规则不在仓库里,无法确认对内网出站是否被拦。
- **现有的地址校验**:`packages/expert-work-common/src/expert_work/common/url_validation.py`。静态检查 `:116-143`;拦的网段 `:42-56` **不含 `100.64.0.0/10`**(阿里云元数据 `100.100.100.200` 在里面);「解析后逐个检查」的函数 `:146-190` 用的是同步 `socket.getaddrinfo`(`:173`),credential-proxy 出口代理在用(`services/credential-proxy/src/credential_proxy/egress_proxy.py:44,61`)。
- **ADR-0009**:`docs/adr/0009-control-plane-egress-ssrf-dns-rebind.md:12-14` 定「DNS 重绑定不在应用层防」,`:66-79` 把出口策略列为部署前提,其中禁止网段(`:70-73`)也没有 `100.64.0.0/10`。
- **用量记在哪**:每次模型调用一行 `token_usage`,`tenant_id` 必填(`packages/expert-work-persistence/src/expert_work/persistence/models/token_usage.py:24`);`platform_overhead` 这一类在运营用量页「按用途」可见、不进客户账单(`packages/expert-work-persistence/src/expert_work/persistence/token_usage_store.py:148-154`)。平台自己的保留租户 `settings.platform_tenant_id`(`settings.py:431-434`)。价目表在 `packages/expert-work-persistence/src/expert_work/persistence/billing/rate_card.py:69` 起。

---

## 3. 业界做法(简)

常见做法(OpenAI / Anthropic 官方 SDK、LangChain、LiteLLM、Dify、One-API 等)一致是「每个凭证 / 每个模型供应商配置一个 base URL,SDK 在后面接 `/chat/completions` 之类的路径」。各家对「要不要带版本段」不统一:OpenAI SDK 默认 `https://api.openai.com/v1`(带),Anthropic、DeepSeek 文档给的是不带 `/v1` 的根(本文作者记忆,开发时再核)。本设计统一要求带版本段,遇到这两家专门提示(§4.5.1)。

「改地址必须同时给新密钥」的出处:协调方 11-16 调研开源平台时看到 Langfuse 的做法 —— 它的模型密钥与地址存在同一条配置里,地址变了而这次没带密钥就报错(报错原文 "Secret key is required when changing the base URL"),起因是社区报告的「改了地址、旧密钥被发到新地址」(#15401)。本文作者没有联网逐条核对,以协调方的调研为准。本项的规则与它同一思路,区别是本项也接受「换成另一个引用」。

---

## 4. 设计

### 4.1 数据

迁移 **0191_b308_credential_base_url**(两张表做同样的事:`platform_provider_secret`、`tenant_provider_secret`):

```sql
ALTER TABLE <t> ADD COLUMN base_url TEXT NULL;                                  -- 空 = 官方地址
ALTER TABLE <t> ADD COLUMN base_url_unverified BOOLEAN NOT NULL DEFAULT false;  -- 「仍然保存」后的标记
ALTER TABLE <t> ADD CONSTRAINT ck_<t>_base_url_https
  CHECK (base_url IS NULL OR base_url LIKE 'https://%');                        -- 兜底;真正的校验在接口层
-- 降级:删约束、删两列
```

- 不回填。老行 `base_url` 为空,行为与今天相同。
- 旧镜像不读不写这两列:`base_url_unverified` 有库级默认值,旧镜像的插入照常成功;旧镜像的整行 upsert 只写它认识的列,不会清掉地址;解析密钥时无视地址(回滚的后果见 §4.10)。
- **密钥存放不变**:`secret_ref` 仍是必填的 `secret://` / `kms://` 引用;粘贴的密钥仍写进密钥库这一行的**固定槽位**(`_canonical_secret_name`,与 `main` 相同:平台 `expert-work/platform/llm/{provider}[/{key_id}]`,租户覆盖 `expert-work/platform/tenant/{tenant_id}/llm/{provider}`),换密钥就地覆盖。
- 数据对象 `PlatformProviderSecretRecord` / `TenantProviderSecretRecord`(`extra="forbid"`)加 `base_url: str | None`、`base_url_unverified: bool = False`;仓库两个 `upsert_*` 各加这两个参数,仍是整行覆盖,不做条件写。
- 迁移测试 `test_migration_0191_b308_credential_base_url.py`:升级后每张表多两列、类型与可空性对;老行两列是空 / false;约束拒 `http://`、放行 `https://` 与空;降级后列和约束都消失、老行还在。

**环境变量里的密钥**(`settings.py:931-934` 的 `platform_provider_credentials`)不变:仍是「厂商 → 引用」,经密钥库取值,只能走官方地址;启动校验不变。要用自定义地址就在页面给这家加一行数据库密钥(之后这家以数据库为准,`platform_secrets.py:160-174`)。`docs/runbooks/credentials.md` 本文作者读不到(工具权限,没有绕开),按代码事实环境变量这条路没有变化,PR 2 改手册的人读一遍确认示例仍是「引用 → 官方地址」。

### 4.2 接口

`PlatformSecretWrite`(`platform_config.py:69-101`)在今天的字段之外加两个:

| 字段 | 意思 |
|---|---|
| `value` / `secret_ref` | **与 `main` 相同**:每次保存恰好给一个。粘贴的原文写进这一行的固定槽位(就地覆盖),引用原样存 |
| `enabled` / `priority` | 与 `main` 相同 |
| `base_url`(新) | **没传 = 保持这一行原来的地址**(切换启用、改优先级不会清掉地址);`null` / 空串 = 清掉、回官方;非空 = 设为这个地址(规范化后,见下) |
| `force_save`(新) | 「仍然保存」:连通测试失败也保存。对格式错误、地址不允许无效 |

前端的「切换启用」照旧把读到的引用原样回传(`apps/admin-ui/src/pages/SettingsPlatformConfig.tsx:280-295`),**不带 `base_url`**。

**改地址必须同时给新密钥(拍板 10)**:

- 「地址变了」= 保存后的地址(规范化之后的整串,空表示官方)不为空,且和这一行保存前的不同 —— 主机、端口、路径任一不同,包括从官方改成自定义;新建行带地址也算。
- 这时这次请求必须**带 `value`**,或者**带一个和这一行当前引用不同的 `secret_ref`**(新建行没有当前引用,给哪个引用都算)。否则 422 `BASE_URL_NEEDS_NEW_KEY`,文案「换地址请同时填写这个服务的密钥」。
- **清空回官方不用**。地址没变(没传,或传了同一个地址)也不用。
- 这条检查在连通测试**之前**做,不满足时一个请求都不会发出去。
- 它只是一条普通的保存前检查:不做比较后写,不防两个人同时保存(§0.1「不防的」)。

**地址怎么规范化**(`credential_base_url.normalize_base_url`):去掉首尾空白;主机名转小写、转 punycode、去末尾的点;端口 443 省略;去掉末尾 `/`;路径大小写保留;主机名里有不可见字符、全角字符、全角点则拒(`BASE_URL_INVALID`)。规范化后正好等于这家的官方根地址 → 存空(等于「回官方」)。存库、比较「变没变」、读视图都用规范化后的整串。

**写入顺序**:

1. 格式、公网检查(§4.5.1)和「改地址必须同时给新密钥」;
2. 连通测试(需要时,§4.5.3),用这次请求里粘贴的原文;给了引用就用这个引用取到的值;都没给就用这一行现有的密钥;
3. 粘贴了原文 → 写进密钥库这一行的固定槽位;
4. 写凭证行(地址、标记一起);
5. 清缓存、清已构建的智能体、广播给别的副本、审计(与 `main` 相同,`platform_config.py:146-173`)。

第 1、2 步失败时什么都没动(密钥库里原来的密钥也没换)。**先写密钥库、后写凭证行**是有意的:新地址只有在新密钥已经就位之后才会被任何副本读到,所以「新地址 + 旧密钥」这个组合不会出现(T3)。第 3 步成功、第 4 步失败时,结果是「旧地址 + 新密钥」:如果旧地址是官方,新密钥被发到官方、调用失败,不外泄;如果旧地址是另一个转发服务,新贴的那把密钥会被发到旧的转发服务 —— 这需要保存过程中数据库出错,管理员会看到报错,重存即可(§0.1「不防的」第 3 条)。

**删除**:与 `main` 相同(删行;引用正好是这一行的固定槽位时顺带删那个槽位,外部引用不碰)。

**工具密钥不收地址**:`/tools/{tool}`、`/tenants/{id}/tools/{tool}` 共用同一个写入体,带了 `base_url` 一律 422 `BASE_URL_NOT_SUPPORTED`。`azure` / `self-hosted` 的密钥同样不收地址(它们的地址来自智能体清单,§7.2),写入行为与 `main` 相同。

错误码(都是 HTTP 422):

| 码 | 什么时候 | 能「仍然保存」吗 |
|---|---|---|
| `BASE_URL_NOT_SUPPORTED` | 厂商是 `azure` / `self-hosted`,或是工具密钥 | 否 |
| `BASE_URL_INVALID` | 不是 https、带用户名密码、带 `?` 或 `#`、没有主机名、超过 2048 字符、主机名含不可见 / 全角字符、HTTP 客户端无法对它发请求 | 否 |
| `BASE_URL_FORMAT` | 版本段不对或带了接口路径(§4.5.1 第 3 条),报文带 `hint`、`expected_suffix`、`example` | 否 |
| `BASE_URL_BLOCKED_HOST` | 主机名或解析出的任一地址不是公网 | 否 |
| `BASE_URL_UNRESOLVABLE` | 域名解析失败或超时 | 否 |
| `BASE_URL_NEEDS_NEW_KEY` | 地址变了,却没粘贴、也没换引用(引用按解析后的名字比:`secret://x`、`secret://x/`、`kms://x` 算同一把) | 否 |
| `SECRET_NOT_FOUND` | 要做连通测试,而这次给的引用在密钥库里不存在 | 否 |
| `BASE_URL_CHECK_UNAVAILABLE` | 这家厂商没有可用来测试的对话模型(目录里都停用了) | 否 |
| `BASE_URL_CHECK_FAILED` | 连通测试失败,`reason` ∈ `unreachable` / `auth_rejected` / `timeout` / `vendor_error`(带 `status`),`interface` ∈ `chat` / `embedding` / `rerank` | **是** |

读视图:每把密钥、每条租户覆盖多返回 `base_url`、`base_url_unverified`;每个厂商多返回 `official_base_url`(§4.4 表第二列;不收地址的厂商为 `null`),前端拿它当占位字,不另抄一份。`secret_ref` 照旧返回。

**审计**:写入的 `details` 照旧记 `secret_ref`,另加 `base_url_host`(主机名,`null` 表示官方)、`base_url_path_fp`(路径的 SHA-256 前 12 位)、`connectivity_check`(`passed` / `forced:<原因>` / `not_run`;强制保存另记 `connectivity_status`、`connectivity_interface`)。**不记完整地址**:有的转发服务把令牌放在路径里;完整地址只存在库里、只在系统管理员的读视图里返回。没存成的也记一条(`saved=false`):连通测试失败记 `connectivity_check=failed:<原因>` 与状态码、接口;`BASE_URL_NEEDS_NEW_KEY`、`BASE_URL_BLOCKED_HOST`、`BASE_URL_UNRESOLVABLE` 记 `refused=<错误码>` 与主机名。

### 4.3 解析顺序:解析器直接给「引用 + 地址」

**顺序**:先按今天的规则选出这次用哪把密钥(有租户覆盖用覆盖,否则平台的多把按优先级),**地址取自选中的那一行**;那一行没填就用官方地址。租户覆盖不填地址 → 官方(拍板 6)。平台多把密钥各用各的地址,切换到哪一把就用哪一把的地址。

**怎么保证地址丢不了**:如果在 `build_llm_router` 加一个「查地址」参数,它上面还有好几层(`build_agent`、`resolve_defenses`、`_build_judge_caller`、辅助模型的构造函数),任一层给它默认 `None` 就会悄悄走官方。所以**让解析器本身返回地址**,凡是拿着解析器的地方自动拿到地址,中间层不需要新参数:

1. 新的值对象 `ProviderKey(secret_ref: str, base_url: str | None)`,放在编排层(`orchestrator/llm/endpoints.py`,编排层不依赖控制面)。
2. `PlatformSecretsService` 的「按优先级的列表」改为返回 `ProviderKey` 列表:`effective_provider_keys()` / `effective_provider_keys_for(tenant_id)` 取代 `effective_provider_secret_refs*`(`platform_secrets.py:75-101`),不留旧名;租户缓存从「引用或 None」改为「`ProviderKey` 或 None」(`:183-187`)。只返回引用的 `effective_provider_credentials*`(`:66-73`、`:108-123`)保留,只剩判断「这家配没配」的用途(`api/model_catalog.py:41`、`api/tenant_config.py:88` 等)。
3. `TenantOverlayCredentialsResolver` 加两个方法:`resolve_provider_key(tenant_id, provider) -> ProviderKey`(最优一把)和 `resolve_provider_key_list(...) -> list[ProviderKey]`;构造参数改名(`tenant_provider_key_view` / `tenant_provider_key_list_view`),老参数名删掉。CI 的 mypy 不扫 `services/control-plane/src` 和 `tools/eval`(`.github/workflows/ci.yml:130-131`),所以靠「改名」(没改过来的调用方一运行就报错)加上结构性测试和每个调用处一个真正执行到的测试(§7.1 第 8 条)。
4. **只给引用的老方法失败即停**:`resolve_provider` / `resolve_provider_keys`(`tenant_secret_overlay.py:49-83`)保留签名,选中的那把密钥**带自定义地址时**抛新的异常 `ProviderKeyNeedsEndpointError`,**不是** `CredentialsResolverError`(后者在 `runtime.py:785`、`:1487`、`:1691` 被接住当成「没配凭证」,那样又成了悄悄降级)。没配地址的密钥行为不变。
5. 编排层的 `ProviderKeyResolver`(`agent_factory.py:2325`)改为返回 `list[ProviderKey]`;`make_provider_key_resolver`(`runtime.py:758-789`)改调新方法。`build_llm_router` 对每个 `ProviderKey` 读密钥、把 `base_url` 交给 `_build_provider`。
6. 第 2.4 节 A 类里「先解析一把、塞进 `api_key_ref`」的调用处(裁判、质量裁判、辅助模型、重排聊天分支)全部改为:调 `resolve_provider_key` 拿到 `ProviderKey`,再用一个「固定这一把」的 `fixed_provider_key_resolver` 交给路由;它只回答这把密钥自己的厂商,别的厂商回空。`api_key_ref` 这条老路只留给测试和离线命令行,天然只走官方。
7. 两条结构性测试守住以后的新代码:控制面非测试代码里不许出现 `resolve_provider(` / `resolve_provider_keys(` 调用(允许名单:覆写定义本身,以及 `tenant_secret_overlay.py` 内部那一处回落)、不许构造带 `api_key_ref` 的 `ModelSpec`。
8. 自己返回字符串列表的脚本跟着改:`tools/eval/b301_health_run.py:291-301`、`tools/eval/skill.py:117-122`、`tools/eval/compaction_retention.py:374-381`;测试里用到这些方法或参数的地方也改。

同一家配两行、一行走转发一行走官方可以配。转发地址挂掉时官方那一行**不会**顶上(下一段),它只在限流、欠费、密钥失效时顶上。屏蔽某个租户用某家厂商的做法不变:建一条停用的租户覆盖(引用随便填一个,停用的行不会被取值)。

**同厂商多把密钥、地址不同时的切换(本项不改)**:路由规则保持今天的样子(§2.6)。自定义地址连不上、超时或返回 5xx 时,平台**不会**改用同一家厂商的另一把密钥(哪怕那把走官方地址),而是直接切到智能体配置的备用模型,没有备用模型就报错;限流、欠费、密钥失效这三类仍切到同厂商的下一把密钥;转发服务返回「请求格式类」的 4xx(`router.py:559-565`)或已经开始输出之后流中断(`router.py:544-548`)时连备用模型也不切、这一轮直接报错。「主密钥走转发、备用密钥走官方,转发挂了自动切到官方」**做不到**,是 B-309 的事。这一点写进表单说明(§4.8)和风险(§7.4)。

**时效与缓存**:写入的那台副本立即生效(清凭证缓存、值缓存、已构建的智能体);其它副本靠广播(`invalidation_bus.py:396-411`,同时清这三样),广播丢了最迟 30 秒凭证缓存到期(`platform_secrets.py:51`)—— 和今天换密钥一样。

**带自定义地址的密钥不进值缓存**(T3 的最后一块):向量化、文档识别、厂商重排取密钥时经 `_cached_secret`(`runtime.py:1333`)查进程内值缓存,按「租户 + 引用」缓存 300 秒(`credential_value_cache.py:25`、`:51`)。换密钥是就地覆盖、引用不变,所以如果某个副本**没收到广播**,30 秒后它读到了新地址,却可能还从值缓存里拿到旧密钥 —— 管理员操作完全正确也会把旧密钥(可能就是公司在厂商的真密钥)发到新地址。改法:`_cached_secret` 收 `ProviderKey`,**`base_url` 不为空时不读也不写值缓存**,每次直接读密钥库;官方地址的密钥照旧走缓存。同一个值缓存还有两处经 `CachingSecretStore` 用在 `build_llm_router` 里:辅助模型(`aux_model_adapter.py`)和质量裁判(`quality_judge.py`)—— 选中的那把带地址时改交原始密钥库。代价:用自定义地址的那几把密钥每次向量化 / 重排 / 识别 / 辅助调用多一次密钥库读。智能体构建时 `build_llm_router` 直接读密钥库(`agent_factory.py:2837`),「地址 + 密钥」在构建时一起取,配合「先写密钥库、后写凭证行」不会配出「新地址 + 旧密钥」。

### 4.4 地址怎么拼(每家厂商)

编排层新表(`orchestrator/llm/endpoints.py`):每家厂商一个**官方根地址**(含版本段),每种用途一个**后缀**。官方地址 = 官方根 + 后缀;自定义地址 = 管理员填的根地址(去掉末尾 `/`)+ 同一个后缀。旧常量(`KIMI_BASE_URL` 等)保留并继续导出,别处还在引用(例如 `tools/eval/kb_eval.py:150`)。

| 厂商 | 表单占位(官方根地址) | 版本段要求 | 对话 | 向量化 | 重排 | 版面解析 |
|---|---|---|---|---|---|---|
| anthropic | `https://api.anthropic.com/v1` | 以 `/v1` 结尾 | `/messages` | — | — | — |
| openai | `https://api.openai.com/v1` | 以 `/v1` 结尾 | `/chat/completions` | `/embeddings`(开发时再核) | — | — |
| kimi | `https://api.moonshot.cn/v1` | 以 `/v1` 结尾 | `/chat/completions` | — | — | — |
| glm | `https://open.bigmodel.cn/api/paas/v4` | 以 `/v4` 结尾 | `/chat/completions` | `/embeddings`(开发时再核) | `/rerank` | `/layout_parsing` |
| deepseek | `https://api.deepseek.com/v1` | 以 `/v1` 结尾 | `/chat/completions` | — | — | — |
| qwen | `https://dashscope.aliyuncs.com/compatible-mode/v1` | 以 `/compatible-mode/v1` 结尾 | `/chat/completions` | `/embeddings` | 见下 | — |
| doubao | `https://ark.cn-beijing.volces.com/api/v3` | 以 `/v3` 结尾 | `/chat/completions` | — | — | — |
| azure / self-hosted | 表单不显示这一项 | — | — | — | — | — |

扫描页识别的「看图转文字」用同一家的对话地址(`clients.py:46-53`)。

**通义的重排**:原生重排接口不在 `/compatible-mode/v1` 下面(`rerank.py:48-49`)。重排地址 = 去掉根地址末尾的 `/compatible-mode/v1`,再接 `/api/v1/services/rerank/text-rerank/text-rerank`。官方情况下拼出来正好是今天的地址。不以 `/compatible-mode/v1` 结尾的通义地址保存时拒绝(拍板 9),提示「通义的地址要以 /compatible-mode/v1 结尾,例如 https://你的地址/compatible-mode/v1」。

**不会出现双 `/v1`,也不会漏版本段**:根地址必须以表里的版本段结尾、且不能已经带上后缀(§4.5.1 第 3 条)。末尾多一个 `/` 自动去掉。

### 4.5 校验

#### 4.5.1 保存时(控制面接口)

1. 厂商不是 `azure` / `self-hosted`,也不是工具密钥。
2. 解析 URL:只许 `https`;不许用户名密码、`?`、`#`;要有主机名;端口可有;长度 ≤ 2048;主机名不许有不可见 / 全角字符。
3. 路径必须以 §4.4 表的「版本段要求」结尾。提示按厂商给:Anthropic / DeepSeek 专门写「这里要带上 /v1(厂商文档里的地址可能不带)」;路径以某个后缀(如 `/chat/completions`)结尾时写「只填到版本号为止,不要带 /chat/completions」;通义写拍板 9 的格式。
4. 主机名本身:拒绝 `localhost` 等本机名、单段名字(没有点)、以 `.local` / `.internal` / `.svc` / `.cluster.local` 结尾的名字、非标准写法的 IP(复用 `url_validation.py:59-76` 的全角点折叠和 `:98-106` 的写法检查)。
5. **异步**解析域名(事件循环的 `getaddrinfo`,5 秒超时,不用 `url_validation.py:173` 那个同步版本),**每一个**地址都必须是公网地址,判定见下;解析失败也拒。
6. 「改地址必须同时给新密钥」(§4.2)。
7. 以上都过,再做连通测试(§4.5.3)。

**「公网地址」怎么判**:先把 IPv4 映射地址(`::ffff:a.b.c.d`)、IPv4 兼容地址(`::a.b.c.d`)、SIIT(`::ffff:0:a.b.c.d`)、NAT64(`64:ff9b::/96`)、6to4(`2002::/16`)里嵌着的 IPv4 拆出来再判;满足任一即拒:`is_global` 为假、组播、未指定,或落在显式名单 `0.0.0.0/8`、`10.0.0.0/8`、`100.64.0.0/10`(含阿里云元数据与内网 DNS)、`127.0.0.0/8`、`169.254.0.0/16`、`172.16.0.0/12`、`192.168.0.0/16`、`198.18.0.0/15`、`::1`、`fc00::/7`、`fe80::/10`、`fec0::/10`。显式名单和 `is_global` 两道都查,因为不同 Python 版本判定有差异;测试在 CI 的 Python 版本上跑。集群的服务网段、pod 网段都在私网段里。没有任何「内网白名单」开关(拍板 11)。

这套判定写在 `expert-work-common`(`url_validation.py` 加 `is_public_address`、`check_public_endpoint` 和异步的 `resolve_public_addresses`),**不改**现有 `validate_remote_url` 的行为(那是 MCP、webhook 等的,§7.3)。

#### 4.5.2 每次调用前(防 DNS 重绑定):在最底层决定

**一个进程级的「只连检查过的地址」客户端**,所有用自定义地址的请求都走它,不管上层传了什么客户端(独立审查 M2):

- **决定放在最底层**:`client_for`(`_http.py:27-60`)加一个参数 `custom_endpoint: bool`。为真时**忽略**传进来的共享客户端或 `None`,一律用这个进程级客户端;只有测试显式给了 `transport` 时用测试的。所有厂商 HTTP 客户端(`openai.py`、`anthropic.py`、`embedder.py`、`rerank.py`、`rerank_cohere.py`)都经 `client_for`;扫描页识别的 `_post`(`clients.py:174-183`)改为也经它。厂商客户端在拼地址时就知道这是不是自定义地址(§4.4 的拼接函数返回「地址 + 是否自定义」),自己把标志带上,上层不用管。质量裁判、辅助模型、评测脚本没传共享客户端的问题(§2.4 末)也就一并解决。
- **连接层怎么做**:在**建立每一条新 TCP 连接时**:异步解析域名 → 所有地址按 §4.5.1 判 → 有一个不合格就整条拒 → 合格的地址**按解析顺序逐个尝试连接**(保留 IPv4 / IPv6 互为后备),连上哪个用哪个。TLS 仍按原域名校验证书(换的只是「连哪个 IP」,握手时的服务器名还是原域名)。透传 `local_address`、`socket_options`。
- **被拒时抛 `httpcore.ConnectError`**:httpx 会把它变成 `httpx.ConnectError`,厂商客户端照旧转成 `LLMNetworkError`(`openai.py:313-314`),路由按今天对网络错误的规则走:跳过同厂商其余密钥,去智能体配置的备用模型(§2.6、§4.3);没有备用模型或都失败才报错给用户,文案是通用的「模型服务地址不可用」,不带地址。
- **和共享客户端一样的参数**:不设默认超时、`keepalive` 64 条、55 秒过期、不限总连接数、禁用 cookie(`app.py:1377-1414`)。另外 `trust_env=False`(不吃环境里的代理变量,否则检查的是代理)、不跟随跳转。
- 已建好、在复用的长连接不再重复解析 —— 它连的就是当初检查过的地址。
- **谁建、谁关**:控制面启动时和共享客户端在同一处创建(`app.py:1377` 旁)、登记给 `client_for`,关停时由同一个退出栈关闭。**不做模块级单例**(测试里事件循环会换,跨循环复用客户端会出错)。没有登记的进程(评测脚本、命令行)里,`client_for` 每次新建一个**带同一套检查连接层**的客户端、用完即关 —— 慢一点,但绝不退回普通客户端。
- 实现提示:httpcore 1.0.9 的连接池收自定义网络后端(`httpcore/_async/connection_pool.py:60`),httpx 0.28.1 的默认传输层没露这个参数,需要写一个小的传输层包装。PR 1 先做小验证。
- 官方地址的调用**完全不走**这个客户端,行为和性能与今天相同。

#### 4.5.3 保存时的连通测试(拍板 8)

- **什么时候测**:这次请求带了非空的 `base_url`(哪怕和原来一样 —— 这是管理员重测、清掉「未通过测试」标记的办法),或者这一行有自定义地址而这次换了密钥(粘贴或换引用)。清空回官方、只改启用 / 优先级时不测。
- **用哪把密钥**:这次粘贴的原文;给了引用就用这个引用取到的值;都没给就用这一行现有的密钥。测完才写密钥库和凭证行(§4.2 写入顺序)。
- **测哪个接口**:默认测对话。这家厂商**没有被任何智能体引用**(读视图的 `used_by_agents`)、平台自身只把它用在向量化或重排上(`platform_uses`)时,测对应接口:向量化发一个短字符串;重排发一个问题 + 两段很短的文本;两者都用时两个都测,任一失败即失败,合计不超过 15 秒。失败时报文带 `interface`,审计带 `connectivity_interface`。
- **用哪个模型**(按顺序取第一个有的):① 引用这家厂商的智能体里已经在用的对话模型(取用得最多的一个);② 平台功能给这家配的对话模型;③ 模型目录里这家没停用的对话模型中,按价目表「输入价 + 输出价」最低的,都没定价取第一个。任何一步都排除向量化 / 重排专用和只用于识别的模型。向量化 / 重排用平台当前配置的那个模型。
- **发什么**:一句极短的话,输出上限取最小档、能关思考就关;整个测试最多 15 秒。只看「厂商有没有正常应答」:对话要有 `choices` / `content`,向量化要返回一条非空向量,重排要有非空结果列表;因输出上限截断也算通过;2xx 但不是这个接口的回包、3xx、解析回包时的任何意外,一律算 `vendor_error`,不会变成 500。
- **失败原因**:连不上(解析 / 连接失败)→ `unreachable`;401 / 403 → `auth_rejected`;超过 15 秒 → `timeout`;其它 4xx / 5xx / 429 → `vendor_error`(带状态码);连接层发现地址不是公网 → 直接按 `BASE_URL_BLOCKED_HOST` 拒,不能强制。
- **「仍然保存」**:页面在失败提示下给一个勾选框,勾了重发并带 `force_save=true`;审计记 `forced:<原因>`。强制保存的行带「未通过测试」标记(`base_url_unverified`):强制保存 → 置真;之后任何一次连通测试通过 → 清掉;清空回官方 → 清掉;只切换启用 / 改优先级 → 保持原值。
- **记用量**:和别的平台调用一样记一行 `token_usage`:`usage_kind = platform_overhead`(不进客户账单,`token_usage_store.py:148-154`),`agent_name = credential_check`;平台密钥记在平台保留租户(`settings.platform_tenant_id`)名下,租户覆盖记在那个租户名下;重排在厂商回包带用量时也记。每次花费以「几十个 token」计。
- 测试请求同样走 §4.5.2 的客户端,保存时的检查和调用时的检查是同一套。

#### 4.5.4 与 ADR-0009 的关系

ADR-0009 说应用层「解析后钉住地址」脆弱、不做。本项按拍板 4 做,理由:① 目标地址是管理员手填、长期存在的配置,出错后果是平台密钥连同对话内容发到内网服务;② 仓库里已有同样做法的先例(`egress_proxy.py:44,61`);③ 只影响自定义地址这一条路,范围小、可测。PR 1 给 ADR-0009 加一节「例外:平台凭证的自定义请求地址」,写明范围和实现方式;同时把 `100.64.0.0/10` 补进它 §2.3 的禁止网段(`:70-73`)。出口网络策略仍是部署前提,两层都要。

### 4.6 调用处清单(全部)

靠 §4.3 的做法,下表里「自动」的那些不需要改代码就拿到地址;「改」的要改成用新方法。每一行在 §7.1 第 8 条都有一个测试。

| # | 入口 | 怎么拿到地址 |
|---|---|---|
| 1 | `make_provider_key_resolver`(`runtime.py:758-789`) | 改:调 `resolve_provider_key_list`,返回 `ProviderKey` 列表 |
| 2 | `build_llm_router` / `_build_provider`(`agent_factory.py:2796-2864`、`:3224`) | 改:按 `ProviderKey.base_url` 拼地址;熔断标识传实际地址(`:2862`);self-hosted / azure 照旧用清单地址 |
| 3 | 智能体构建(`runtime.py:1287`)、子智能体(`subagent_runtime.py:625`、`:852`)、看图 / 升级 / 分步路由(`agent_factory.py:760-855`、`:2902-2974`) | 自动(经 #1) |
| 4 | 长期记忆写回(`writeback_processor.py:452-469`) | 自动(经 #1) |
| 5 | 裁判 `_build_judge_caller`(`runtime.py:886`、`:916`),经 `resolve_defenses` 从 `runtime.py:1272`、`subagent_runtime.py:610`、`:837` 进入 | 改:`resolve_provider_key` + 固定一把的 key resolver;三个入口自动 |
| 6 | 质量裁判(`quality_judge.py:126-145`) | 改:同 #5 |
| 7 | 辅助模型(`aux_model_adapter.py:113-155`),入口:委派策略 `delegation_policy.py:184`、知识库画像 `app.py:2297`、记忆整理 `app.py:2438`、技能演化 `app.py:2515`、评测 `b300_inpod.py:628` | 改:同 #5;五个入口自动 |
| 8 | 重排聊天分支(`runtime.py:1518-1527`、`:1728`) | 改:同 #5 |
| 9 | 向量化(`runtime.py:1384-1397`、`:1603-1611`;评测 `b300_inpod.py:616-624`) | 改:`resolve_provider_key`,按 §4.4 拼 `/embeddings` |
| 10 | 厂商重排(`runtime.py:1484-1510`、`:1688-1707` → `rerank_vendors.py:105-133`) | 改:`vendor_reranker` 收 `ProviderKey`,通义按 §4.4 推重排地址、智谱接 `/rerank` |
| 11 | 扫描页识别与图片描述(`ResolvingProviderKeys`,`runtime.py:1401-1414`;`recognizer.py:329-340`、`figure_pipeline.py:111-117`) | 改:返回「密钥 + 根地址」,按根地址拼,不再查写死的表 |
| 12 | 评测脚本 `tools/eval/b300_inpod.py:604-613`(自建解析器)、`tools/eval/compaction_retention.py:374-381`(自己从服务取引用列表) | 改:随改名后的服务方法 / 构造参数更新。它们在集群里用真实平台凭证跑,**在本项范围内**;各配一个真正执行到这段代码的测试 |
| 12b | 评测脚本 `tools/eval/b301_health_run.py:291-301`、`tools/eval/skill.py:117-122`(自己写的 key resolver,返回字符串列表,用的是脚本自带的密钥、不是平台凭证) | 改:返回 `ProviderKey`(地址为空);只是跟着类型走,不涉及自定义地址 |
| 13 | 所有厂商 HTTP 客户端经 `client_for`(`_http.py:27`)+ 识别 `_post`(`clients.py:174-183`) | 改:按 §4.5.2 在最底层选客户端 |

限流桶、重排节奏仍按引用分,**不改**(§2.6);密钥值缓存除「带自定义地址的密钥不进缓存」(§4.3)外不改。只读型的「这家配没配」检查(§4.3 第 2 条)不改。

### 4.7 能看到「在用自定义地址」,且不泄露地址里的东西

- **页面**:密钥表加一列「请求地址」,显示「官方」或自定义地址的主机名;租户覆盖表同样一列。
- **`httpx` 日志去掉路径**:`init_logging`(`log.py:193` 起)里给 `httpx` 日志加一个过滤器,把 `HTTP Request: …` 这一行里的地址改写成只剩「协议 + 主机 + 端口」,其余照旧打;`httpcore` 日志级别固定为 WARNING。过滤器在两处安装、重复安装无害:`init_logging` 里,以及「只连检查过的地址」客户端所在模块被加载时(覆盖不调 `init_logging` 的评测脚本、命令行)。地址参数是 `httpx.URL` 对象或字符串都处理。
- **平台自己的日志**:连接层被拒时一条 `provider_endpoint.blocked provider=… host=… reason=…`,只记主机名。异常信息里如果带地址,自定义地址的请求在抛出前把地址改写为只有主机。
- **审计**:见 §4.2,记主机名 + 路径指纹,不记完整地址。
- **指标**(都不带地址):`expert_work_platform_credentials_custom_endpoints`(填了地址的行数,与已有的租户覆盖计数同一处设置);`expert_work_custom_endpoint_connections_total{provider, outcome}`(`ok` / `blocked` / `unresolvable` / `connect_error`);向量化、重排的 span 加属性 `endpoint=custom`。

### 4.8 页面

编辑 / 新增密钥弹窗、租户覆盖弹窗都加一项(放在「启用」之前):

- 标签:**请求地址(可选)**
- 占位:这家厂商的官方根地址(来自接口的 `official_base_url`)
- 说明:「不填就用官方地址。只填到版本号为止,例如 `https://你的地址/v1`,平台会在后面接上具体接口路径。只支持公网 https 地址;地址里不要放密钥。用这把密钥的所有调用(对话、看图、向量化、重排、文档识别等)都会发到这里。改地址时请同时填写这个服务的密钥。保存时会用这把密钥真实调用一次来测试。这个地址连不上、超时或出错时,平台不会改用同一家的其它密钥,而是切到智能体配置的备用模型;如果这个地址返回 400 / 404 一类的错误,或回答输出到一半中断,这一轮会直接报错、不会切换。」
- 地址改了(和打开弹窗时不同、且不是清空)而密钥框空着、引用也没改 → 前端先行提示「换地址请同时填写这个服务的密钥」,不发请求(后端同样会拒)。
- 新建一行(或新建租户覆盖)并填了地址时,在密钥框旁提示「请填写这个服务的密钥」(新建行用引用也能保存,提示是为了避免顺手填成厂商那把)。
- 连通测试失败时,在表单下方显示原因(连不上 / 密钥被拒 / 超时 / 厂商报错 + 状态码,以及哪个接口),并出现勾选框「仍然保存」;「地址不允许」和格式错误不出现这个勾选框。
- `azure` / `self-hosted` 不显示这一项(`official_base_url` 为 `null`)。
- 「切换启用」「改优先级」请求不带 `base_url`;编辑弹窗里地址没改也不带(带了非空地址每次都会真测一次)。
- 中英文案进 `settings_platform` 块(`zh-CN.ts:2775` 起,英文同名块);错误码映射成人话(§4.2 表)。

**给 PR 2 的对外变化(相对 `main`;以这张表为准)**:

| 项 | 后端行为 | 前端要做的 |
|---|---|---|
| 读视图 | 每把密钥 / 每条租户覆盖多 `base_url`(规范化形式,官方为 `null`)、`base_url_unverified`;每个厂商多 `official_base_url`(不收地址的厂商为 `null`)。`secret_ref` 照旧 | 加「请求地址」列(主机名或「官方」)与「未通过测试」标记;占位用 `official_base_url` |
| 写入体 | 多 `base_url`(没传 = 保持;`null` / 空串 = 清掉)、`force_save`。`value` / `secret_ref` 二选一与 `main` 相同 | 弹窗加字段;地址没改就不带 `base_url` |
| 保存接口的返回 | `data` 是保存后的行,多 `base_url`、`base_url_unverified` | 以返回值刷新这一行 |
| 切换启用 / 改优先级 | 与 `main` 相同(回传读到的引用) | 不带 `base_url` |
| 改地址 | 必须同时粘贴或换引用,否则 422 `BASE_URL_NEEDS_NEW_KEY`;清空回官方免填 | 先行提示;映射文案;新建行带地址时提示填写这个服务的密钥 |
| 422 `SECRET_NOT_FOUND` / `BASE_URL_CHECK_UNAVAILABLE` | 见 §4.2 表(以前是 500) | 按码提示 |
| 422 `BASE_URL_CHECK_FAILED` | 带 `reason`、`status`(可能为空)、`interface` | 显示原因与接口,给「仍然保存」(重发带 `force_save=true`) |
| 422 `BASE_URL_INVALID` / `BASE_URL_FORMAT` / `BASE_URL_BLOCKED_HOST` / `BASE_URL_UNRESOLVABLE` / `BASE_URL_NOT_SUPPORTED` | 见 §4.2 表;`BASE_URL_FORMAT` 带 `hint`(`includes_api_path` 带了接口路径 / `version_segment` 版本段不对 / `version_segment_required` 厂商文档的地址不带版本段、这里要带)、`expected_suffix`、`example` | 按码提示,不给「仍然保存」 |
| 工具密钥写入 | 带 `base_url` → 422 `BASE_URL_NOT_SUPPORTED` | 工具弹窗不显示地址 |
| 删除(厂商 / 工具) | 与 `main` 相同 | 无 |
| 审计 | 多 `base_url_host`、`base_url_path_fp`、`connectivity_check` 等;没存成的也记一条 `saved=false`(含 `BASE_URL_NEEDS_NEW_KEY`、`BASE_URL_BLOCKED_HOST`、`BASE_URL_UNRESOLVABLE` 与连通测试失败) | 无 |
| `docs/runbooks/canonical-agent-e2e-test.md:128` | 粘贴后读回的 `secret_ref` 仍是固定槽位名,不变 | 无需改 |

### 4.9 向量化按厂商取地址(拍板 7,PR 1 里单独一个提交)

- 现状:`HTTPEmbeddingClient` 默认通义地址(`embedder.py:148`),两处构造不传地址(`runtime.py:1394,1611`);目录里 OpenAI(`packages/expert-work-protocol/src/expert_work/protocol/model_catalog.py:421-422`)、智谱(`:653-654`)也有向量模型,平台向量化配置允许选它们(`services/control-plane/src/control_plane/api/platform_embedding_config.py:200-213`)。
- 修法:按厂商从 §4.4 表取官方向量化地址(通义不变)。这个提交**只做这件事**,不碰请求地址功能。仓库合并 PR 用 squash,合入后它不再是单独的提交;需要单独回退时,按它涉及的文件做 revert:`services/orchestrator/src/orchestrator/llm/embedder.py`、`services/orchestrator/src/orchestrator/llm/endpoints.py` 里向量化地址那部分、`services/control-plane/src/control_plane/runtime.py` 两处构造向量化客户端的地方,以及测试 `services/orchestrator/tests/test_embedding_endpoint_by_provider.py`、`services/control-plane/tests/test_embedding_endpoint_by_provider.py`、`services/control-plane/tests/test_dynamic_resolver.py`。
- 影响:平台向量化选的是通义的部署,行为不变;选了 OpenAI / 智谱的部署,今天每次向量化都会失败(密钥发错了家),修完才开始正常 —— 也就是说,以前根本没成功写入的向量会开始写入。发测试前先用只读查询确认测试 / 生产的平台向量化配的是哪家(生产查询交用户执行)。
- 验收单独一条(§8 A9)。

### 4.10 发布与回滚

- **发布前**:开发时核一次迁移号 `0191` 仍未被别的分支占用;不需要别的数据预查(本版没有改变任何存量行的行为)。
- **发布**:迁移 0191 先上(只加列和约束,所有老行都满足约束,旧镜像照常读写)→ 滚动换镜像。**滚动完成前不要给任何密钥配地址**:还没换镜像的副本无视地址,会把那把密钥发到官方地址(调用失败,不外泄,但那段时间用不了)。
- **回滚镜像(不降级迁移)**:旧镜像无视 `base_url`,配了地址的密钥会被发到**官方**地址 —— 给转发服务的密钥打官方会失败,不会泄露给第三方;但那几家厂商在回滚期间用不了。所以**回滚前先把配了地址的行清空地址**(首选清空;只停用不够:停用的行在旧镜像上可能被重新启用,再升级回来时地址又生效),用下面这条只打印行数的 SQL 确认为 0(停用的行也算):

  ```sql
  SELECT 'platform_provider_secret' AS t, count(*) FROM platform_provider_secret
   WHERE base_url IS NOT NULL
  UNION ALL
  SELECT 'tenant_provider_secret', count(*) FROM tenant_provider_secret
   WHERE base_url IS NOT NULL;
  ```

  旧镜像上的切换启用、换密钥不会清掉库里的地址;再升级回来时地址还在,届时按需重新核对。
- **降级迁移**:删约束、删两列,地址随之丢失,密钥不受影响。
- 以上写进迁移文件的说明、测试手册(`docs/runbooks/pending-test-release-acceptance.md` 的 B-308 一节)和发布说明。

---

## 5. 不做什么

- §0.1「不防的」那几类:恶意的系统管理员(有意把真密钥的引用配上转发地址、利用并发 / 缓存 / 滚动发布 / 回滚的时间差)、能直接改库或读密钥库的人、保存中途出错留下的半截状态。不为它们加比较后写、版本化槽位、行内加密或回收。
- 聚合平台(OpenRouter 等)、目录外的模型名、价格随地址变化(用户 11-16 明确收窄)。
- 内网地址白名单(拍板 11)。
- 环境变量里的密钥带地址(§4.1)。
- 按地址单独限流:转发服务自己的限流由它报 429,平台按今天的 429 处理。
- 智能体清单里的 `self-hosted` / `azure` `base_url`(§7.2,只记风险)。
- 修 `validate_remote_url` 漏拦 `100.64.0.0/10`(§7.3,只记录)。
- 路由切换规则(B-309)。
- 不经平台凭证的离线评测脚本(例如 `tools/eval/kb_agent_eval.py:157-158` 从环境变量读密钥和地址)不改;用平台凭证的两份在范围内(§4.6 #12)。

---

## 6. PR 拆分与工期

| PR | 内容 | 依赖 |
|---|---|---|
| **PR 1 后端** | **提交 1**:向量化按厂商取官方地址(§4.9)+ 它的测试。**之后**:迁移 0191 与迁移测试;数据对象 / 仓库 / 接口字段、校验、改地址必须同时给新密钥、写入顺序、连通测试与用量记账;`ProviderKey` 与解析器改造、老方法失败即停、两条结构性测试;编排层地址表;最底层的「只连检查过的地址」客户端;§4.6 全部调用处(含四份评测脚本);自定义地址的密钥不进值缓存;熔断标识;日志过滤;指标;审计字段;ADR-0009 补节;测试手册 B-308 一节;单元测试(§7.1) | 无 |
| **PR 2 前端 + 手册** | 按 §4.8 的表改前端:弹窗字段、占位、页面列与「未通过测试」标记、改地址先行提示、连通测试失败提示与「仍然保存」、切换启用不带地址、中英文案、错误提示;前端测试;`docs/runbooks/credentials.md` 核对 | PR 1 |

两个 PR 可同一班发测试。没有总开关:不填地址的行为与今天相同(向量化那一个提交除外,见 §4.9)。

**PR 1 已完成**(分支 `feat/b308-credential-base-url-backend`):第 8 版实现里的版本化槽位、回收、外部引用扫描、比较后写等已删掉,按本版的规则与写入顺序改完;见 §11。剩下的是 PR 2 前端与手册验收用例,约 1 个工作日。

---

## 7. 风险与测试

### 7.1 测试(单元,带「改坏了会红」的变异点)

1. **官方地址逐字节不变**:每家厂商 × 今天在用的每种用途,用**真实的厂商客户端**加 `httpx.MockTransport` 截下实际发出的地址,断言等于 §2.5 表的旧值。变异:改任一后缀或根地址 → 红。
2. **自定义拼接**:`https://p.example.com/v1/` + 对话 → `https://p.example.com/v1/chat/completions`;通义 `https://p.example.com/x/compatible-mode/v1` 的重排 → `https://p.example.com/x/api/v1/services/rerank/text-rerank/text-rerank`。变异:不去末尾 `/`、通义不剥段 → 红。
3. **保存校验表**:`http://…`、`https://u:p@h/v1`、带 `?a=1`、带 `#x`、末尾 `/chat/completions`、Anthropic / DeepSeek 缺 `/v1`(看提示里有专门的话)、通义 `https://h/v1`、`azure` 带地址、工具密钥带地址;IP:`127.0.0.1`、`10.1.2.3`、`172.16.41.73`、`192.168.0.1`、`169.254.169.254`、`100.100.100.200`、`198.18.0.1`、`224.0.0.1`、`::1`、`fd00::1`、`fe80::1`、`fec0::1`、`::ffff:10.0.0.1`、`::10.0.0.1`、`64:ff9b::a00:1`、`2002:a00:1::`;名字:`localhost`、`api`、`x.svc`、`x.svc.cluster.local`、`x.internal`、全角点写的 IP、含不可见字符的主机;解析结果「一个公网一个内网」→ 全拒;解析超时 → 拒;正常公网域名 → 过;填的正好是官方根地址 → 存空。逐条删掉对应判断都应变红。在 CI 的 Python 版本上跑。
4. **重绑定与连接层**:解析桩第一次给公网、第二次给内网;保存通过,调用时新连接被拒,抛 `httpcore.ConnectError`、上层收到 `LLMNetworkError`、路由按第 9 条走。另断言:连接层连的是检查过的 IP、TLS 服务器名是原域名;两个公网地址、第一个连不上时用第二个;`local_address` / `socket_options` 被透传;没有登记进程级客户端时 `client_for` 新建的客户端仍带检查连接层。变异:改回「按域名连」、只试第一个地址、抛别的异常类型 → 各自变红。
5. **没传 = 保持,传空 = 清掉**:先存地址,再发一个不带 `base_url`、回传原引用的保存(模拟切换启用)→ 地址还在、不测;发 `null` / 空串 → 清掉、不测。变异:把「没传」当 `null` → 红。
6. **改地址必须同时给新密钥**(平台密钥、租户覆盖各一组):官方 → 自定义,不粘贴、引用不变 → 422 `BASE_URL_NEEDS_NEW_KEY`;自定义 A → B(换主机 / 只换端口 / 只换路径)不粘贴、引用不变 → 422;同上但粘贴了原文 → 过;同上但换成另一个引用 → 过;新建行带地址 + 引用 → 过;地址不变(传同一个地址,或主机只差大小写 / 末尾点 / `:443`)、不粘贴 → 过(并重测);清空回官方、不粘贴 → 过。每个 422 都断言**没有任何请求发出去**、密钥库和凭证行都没变、审计有一条 `refused=BASE_URL_NEEDS_NEW_KEY`。变异:只比较主机不比较路径、把引用相同也当「换了」、把检查挪到连通测试之后、新建行也要求粘贴 → 各自变红。
7. **连通测试与写入顺序**:桩厂商分别回 200 / 401 / 慢于 15 秒 / 500 / 连不上 → 通过 / `auth_rejected` / `timeout` / `vendor_error` / `unreachable`;失败时凭证行不变,**密钥库里原来的密钥也不变**(先放一个旧值,失败后读出来还是旧值);测试用的是请求里的新明文、或新引用取到的值,不是库里的旧值;成功时**先写密钥库、后写凭证行**(记录调用顺序;变异:顺序反过来 → 红);`force_save=true` 时写库且审计记 `forced:`;地址不允许时 `force_save` 无效;模型选择顺序;有自定义地址的行只换密钥也会测;「未通过测试」标记的置位 / 清除 / 保持;只用于向量化 / 重排的厂商测对应接口、两者都用时两个都测;2xx 但不是这个接口的回包、空向量、空重排结果判失败而不是 500;用量写了一行 `platform_overhead`、租户归属正确。
8. **每个调用处都拿到地址**:§4.6 #3~#12 每一行一个测试,断言请求打到自定义地址;两份用平台凭证的评测脚本各有一个真正执行到构造解析器 / 取密钥那段代码的测试。再加:老方法 `resolve_provider` 遇到带地址的密钥抛 `ProviderKeyNeedsEndpointError`,且穿过 `runtime.py` 三处 `except CredentialsResolverError` 不被吞;`fixed_provider_key_resolver` 对别的厂商回空;两条结构性测试。
9. **多把密钥各用各的,切换规则不变**:主密钥带自定义地址、备用不带;熔断标识不同;主密钥被限流 / 欠费 / 失效 → 试备用,打到备用自己的地址;主密钥遇到网络错误 / 5xx / 连接被拒 → **不试备用**(把现状钉住,B-309 要改时会先红)。
10. **日志不漏路径**:用真实的 httpx 客户端发一次自定义地址请求和一次被拒的请求,在 INFO 级别(`httpcore` 再测 DEBUG)捕获所有日志器的输出,路径里的标记串一条都不出现、主机名出现;不调 `init_logging`、只加载客户端模块时过滤器也在;装两次只有一个;`httpx.URL` 对象参数同样被改写。变异:去掉过滤器 → 红。
11. **只改地址也会让已构建的智能体失效**:保存后 `_invalidate_built_agents` 被调用、广播事件发出。
12. **租户覆盖**:覆盖带地址 → 用它;覆盖不带 → 官方(即使平台密钥带地址);停用的覆盖 → 屏蔽照常生效。
13. **迁移**:`test_migration_0191_b308_credential_base_url.py`(§4.1)。
14. **向量化按厂商**(提交 1):OpenAI / 智谱 / 通义各自打到 §4.4 的官方向量化地址。变异:改回固定通义 → 红。
15. **带地址的密钥不进值缓存**(`_cached_secret`、辅助模型、质量裁判各测):同一个引用,`base_url` 为空时第二次取值命中缓存(密钥库只读一次);`base_url` 不为空时每次都读密钥库、缓存里没有它;模拟「没收到广播」:值缓存里放着旧值、密钥库里已是新值、`ProviderKey` 带新地址 → 取到的是新值。变异:去掉这个判断 → 红。

### 7.2 记录的风险:清单里的 `self-hosted` / `azure` 地址(不在本项)

逐处查过(在 `packages/`、`services/control-plane/src`、`services/orchestrator/src`、`services/credential-proxy/src` 的非测试代码里搜 `base_url`,共 17 个文件;除下面几处外,都是厂商客户端自己的地址字段,或 searxng / keycloak / 沙箱 / 工作区 / 内部 mTLS 等无关用途):

- 字段定义 `packages/expert-work-protocol/src/expert_work/protocol/agent_spec.py:141-146`,协议层没有任何校验器引用它。
- 控制面源码里没有一处读 `ModelSpec.base_url`(出现 `base_url` 的 5 个文件 `app.py / keycloak/admin_client.py / keycloak/token.py / runtime.py / settings.py` 都是别的用途)。
- 编排层只检查「有没有填」:`agent_factory.py:3384-3385`、`:3398-3402`;然后直接用(`:3387-3389`、`:3404-3411`)。不限协议、不查内网。
- 用的密钥是平台给这家配的(`agent_factory.py:2819`、`:2833`)。
- 控制台没有这个字段的表单,只能在清单原文里写(`apps/admin-ui/src/components/manifest-editor/groups/ModelRoutingSection.tsx:34-36`)。

所以:**只要平台配了 `self-hosted` 或 `azure` 的密钥**,任何能改清单的人都能让平台带着这把密钥和对话内容发到他指定的任意地址(含 http 和内网)。没配这两家密钥的部署不受影响;测试 / 生产是否配了,仓库里查不到。建议另开一项:把 §4.5 的同一套校验用到清单地址上,或把这两家的地址也收到平台凭证上。

### 7.3 记录的风险:现有静态检查漏 `100.64.0.0/10`

`url_validation.py:42-56` 不拦 `100.64.0.0/10`,阿里云元数据 `100.100.100.200` 在里面。MCP 注册、webhook 等用它(`services/control-plane/src/control_plane/api/mcp_servers.py:518`、`api/webhook_endpoints.py:124` 等)。本项的新判定会拦,但不改旧函数;建议另开一项。

### 7.4 其它风险

- **管理员有意绕过「改地址必须同时给新密钥」**:例如新建一行、填公司真密钥的引用、配上转发地址。规则只防忘记,不防有意(§0.1);审计记了谁、哪个主机、哪个引用。
- **共用同一个槽位的行跟着变**(与 `main` 相同):别的行(第二把密钥、某条租户覆盖)如果用引用指向这一行的固定槽位,这一行粘贴新密钥时它们也拿到新密钥。如果这一行改成转发地址并贴了转发服务的密钥,那些走官方地址的行会带着转发服务的密钥打官方,调用失败(不外泄)。表单说明里不展开;排查时看审计里的 `secret_ref`。
- **清空回官方不用重填密钥**:如果这把密钥是转发服务自己发的(不是厂商发的),清空后它会被发往厂商官方地址,调用失败(不外泄)。管理员应同时换上厂商的密钥。
- **保存中途出错**:先写密钥库、后写凭证行(§4.2),最坏是「旧地址 + 新密钥」;旧地址是另一个转发服务时,新密钥会被发到旧转发服务。管理员看到报错重存即可。
- **转发服务看得到密钥和全部内容**:这是管理员的选择;表单说明写清,审计记谁改的、改到哪个主机。
- **转发服务行为不同**:例如把流式回包攒着一起发,会触发首 token 超时;或吞掉某些请求头。连通测试只测一句短话、不带流式,发现不了,靠测试环境验收(§8)发现。
- **漏接一处**:靠解析器直接给地址(§4.3)、老方法失败即停、两条结构性测试、§7.1 第 8 条逐处测试兜住。最坏情况是那一处报错,不会悄悄发到官方。
- **每条新连接多一次解析**:有长连接复用,影响小;解析慢会算进首 token 时间。带地址的密钥每次向量化 / 重排 / 识别多一次密钥库读(§4.3)。
- **转发挂了不会切到同厂商的官方密钥**:见 §4.3;想靠「备用密钥走官方」兜住转发故障的管理员会失望,表单说明里直接写明;真要兜,给智能体配备用模型。改这条规则是 B-309。
- **强制保存**:「仍然保存」的行除了审计,在页面上带「未通过测试」标记,直到下一次测试通过。
- **连通测试的花费**:每次几十个 token,记在 `platform_overhead`;厂商若按次计费或有最低消费,实际花费以厂商为准。
- **本地开发**:有的本地代理把公网域名解析到 `198.18.0.0/15`,会被拦;本地测试用桩,不受影响。
- **Python 版本差异**:`ipaddress` 判定随版本变,测试必须在 CI 版本上跑。
- **`httpx` 日志变化**:所有 httpx 请求日志从完整地址变成「协议 + 主机 + 端口」,依赖完整路径排查问题的人需要知道(PR 说明里写)。
- **回滚**:见 §4.10;回滚期间配了地址的厂商用不了,不外泄。

---

## 8. 测试环境验收(写进测试手册新的一节)

**前置**(开发完成后由执行人 —— 开发方 —— 准备,验收完删除;本文不代做):

- **反向代理**:在测试集群里临时部署一个 nginx(一个 Deployment + Service),路径前缀规则 `/b308-proxy/glm/` → `https://open.bigmodel.cn/`(原样透传请求头、关掉响应缓冲以保留流式),再在测试环境的公网入口(ALB Ingress,`infra/k8s/overlays/test/ingress-patch.yaml`)上加一条 `/b308-proxy` 前缀路由指向它。nginx 开访问日志,能按路径数请求。**先确认控制面 pod 里解析这个公网域名得到的是公网地址**;如果集群内解析成内网地址,会被拦 —— 那就换一台有公网证书的外部机器做代理。
- 选智谱做代理对象,因为对话、重排、版面解析都能覆盖到。**执行前先用只读查询看测试环境平台向量化 / 重排 / 识别配的是哪家**:是智谱就在 A3 里一并验;不是的话,A3 只验智谱能覆盖的部分。
- 测试环境有一个用智谱的智能体、一个绑了知识库的智能体。
- 费用:十来次小调用 + 几次连通测试,预计不到 ¥1。

| 编号 | 做什么 | 判据 |
|---|---|---|
| A1 | 给智谱的某把密钥填上 `https://<测试域名>/b308-proxy/glm/api/paas/v4`,同时重新粘贴密钥,保存 | 连通测试通过、保存成功;代理日志有一次 `/chat/completions`;页面「请求地址」列显示主机名;审计里有主机名和路径指纹、没有完整地址;用量页「按用途」多一行 `platform_overhead`,`agent_name` 为 `credential_check` |
| A2 | 用这把密钥的智能体问一句话 | 正常答复;代理日志出现 `/chat/completions` |
| A3 | 看图、知识库检索(带重排)、上传一页扫描件 | 代理日志依次出现对话、`/rerank`、`/layout_parsing`(以及 `/embeddings`,若测试环境向量化是智谱) |
| A4 | 依次保存(每次都粘贴密钥):`http://<测试域名>/b308-proxy/glm/api/paas/v4`、`https://127.0.0.1/api/paas/v4`、`https://100.100.100.200/api/paas/v4`、一个 nip.io 一类的「解析到 `10.x` 的公网域名」、`https://<测试域名>/b308-proxy/glm/api/paas/v4/chat/completions`、`https://<测试域名>/b308-proxy/glm`(缺版本段) | 六个全部被拒,提示依次是「只支持 https」「地址不允许」「地址不允许」「地址不允许」「只填到版本号为止」「要以 /v4 结尾」;都没有「仍然保存」;之后用原密钥的智能体照常能答(密钥没被换掉) |
| A5 | 对 A1 那一行:① 把路径改一下(例如 `/b308-proxy/glm2/api/paas/v4`),不贴密钥、引用不变;② 同样改路径,同时粘贴密钥;③ 给某个租户建覆盖,填平台那把的引用 + 代理地址(新建行) | ① 被拒「换地址请同时填写这个服务的密钥」,代理日志没有新请求;② 走连通测试(路径不存在时失败,符合预期);③ 允许保存(新建行给了引用,按 §4.2 算「给了密钥」;这是 §0.1「不防的」情形,只验证行为符合设计) |
| A6 | 把 nginx 副本数缩到 0(公网入口还在,会回 502 / 503),对 A1 那一行重新发同一个地址(重测) | 连通测试失败「厂商报错(502 或 503)」;勾「仍然保存」后保存成功,审计记 `forced:vendor_error`,页面这一行带「未通过测试」 |
| A7 | 同厂商加一把备用密钥(不填地址、优先级更低);准备两个智能体:甲配了别家的备用模型,乙没配。nginx 保持 0 副本,各问一句 | 甲:答复来自备用模型;乙:这一轮明确报错。两种情况下官方地址的那把备用密钥都**没被用到**(§4.3 现有行为)。nginx 恢复后再问:等熔断恢复之后回到代理(恢复后头一两次仍走备用模型不算红) |
| A8 | 清空地址(不贴密钥) | 保存成功、不测;再问一句,代理日志没有新请求 |
| A9 | (向量化修复,单独一条)**不改**平台向量化配置;在控制面 pod 里用平台的向量化组件对智谱 `embedding-3` 嵌一句话 | 请求发到智谱官方向量化地址并成功;另做一次知识库检索,确认当前平台向量化行为不变 |
| A10 | 控制面进程里 langfuse 把 `httpx` 日志器调到了 WARNING,直接搜日志证明不了过滤器。在控制面 pod 里起一段一次性的 Python,先 `init_logging`、再把 `httpx` 日志器临时调到 INFO,用平台的 `client_for(custom_endpoint=True)` 对代理地址发一次请求 | 输出里有 `HTTP Request: POST https://<测试域名> …`(只有协议 + 主机),没有 `/b308-proxy/` |
| A11(可选,需要能改 DNS) | 保存一个自己控制的域名,之后把它改解析到内网地址,等缓存过期再问 | 新连接被拒;之后的走法同 A7 |
| A12(回滚核对的演练) | 在测试环境跑 §4.10 那条只打印行数的 SQL | A8 之后两张表都为 0;A1 之后、A8 之前为 1 |

---

## 9. 通用性自检

- 表单、报错、日志文字不提任何租户、不偏向哪家厂商;各厂商的差异只在 §4.4 那张表里,按厂商类型而不是按某个客户定。
- 校验规则对所有厂商相同;版本段要求来自各家接口结构,通义多一条是因为它的重排接口结构,不是业务原因。

---

## 10. 还没定的

没有。本版里由开发方按规则做的几处取舍(列出便于审查,不需要用户定):

1. **`key_id` 格式校验去掉**,回到 `main`:它原本是为了版本化槽位名不撞名,槽位方案撤掉后没有理由留,留着反而多一条对外变化。
2. **错误码改名** `BASE_URL_NEEDS_PASTED_KEY` → `BASE_URL_NEEDS_NEW_KEY`,去掉 `hint`:规则从「必须粘贴」变成「粘贴或换引用」,旧名会误导;PR 2 还没开工,改名没有兼容负担。
3. **带自定义地址的密钥不进值缓存**(§4.3):不加它,管理员操作完全正确、只是某个副本没收到广播,也会把旧密钥发到新地址,这正是 T3 要防的结果;改动约 15 行。
4. **`azure` / `self-hosted` 的「必须粘贴」撤掉**,回到 `main`:那条是为防「有意用引用把密钥绑到清单地址」,属于 §0.1「不防的」。

---

## 11. 各版改了什么

**第 10 版·简单版(11-17)**

**为什么收窄(两次,都是用户 11-17)**:

- 第一次:用户问本项为什么这么复杂。第 6~9 版为了守「库里的一把密钥只会被发到粘贴它时的那个地址」,先后做了版本化槽位 + 比较后写 + 对账回收 + 外部引用扫描(第 6~8 版,四轮审查都卡在回收上),又改成把密钥加密存进凭证行、用 AAD 绑定地址(第 9 版、半个第 10 版)。这些机制防的大多是「恶意管理员利用并发、缓存、回滚的边角」。用户定:不再为极端情况加机制,先收到「中间版」(Langfuse 做法:密钥与地址同一行、改地址必须重贴)。
- 第二次:用户进一步定为「简单版」—— 系统管理员可信,只防手误(填错地址、填内网地址、改成转发地址却忘了换密钥),密钥存储不改结构。

**本版删掉的(相对第 9 版与半个第 10 版的设计,以及分支上第 8 版的代码)**:版本化槽位与 `credential_slots.py`;`.retired` 标记、对账回收与宽限期、两项待回收指标;外部引用扫描(`list_foreign_slot_refs`、14 处来源)与密钥库的 `list_names`;`CREDENTIAL_SLOT_REFERENCED_BY_NAME`;比较后写与 409 `CREDENTIAL_CHANGED_CONCURRENTLY`(含「旧页面带旧引用」「删后靠引用复活」);删除审计的 `secret_retired`;发布前三项数据预查;`azure` / `self-hosted` 必须粘贴;`INVALID_KEY_ID`;行内加密(密文 / nonce / KEK 版本 / 密钥代号四列、HKDF 子密钥、AAD 绑定地址、重新加密回官方、`INLINE_KEY_UNAVAILABLE`);两个保留占位引用与屏蔽行;`SECRET_REF_RESERVED`;`BASE_URL_OFFICIAL_HOST`;「只收 ASCII 主机名」(回到规范化成 punycode)。

**本版保留的(已写好、审过)**:地址规范化(等于官方根存空、主机小写、punycode、不可见 / 全角字符拒);只许公网 https 的保存时校验(含 IPv6 内嵌 IPv4 的拆解、`fec0::/10`、解析超时);每条新连接都检查的出站客户端;连通测试(15 秒、两接口都测、`interface`、意外归 `vendor_error`、「仍然保存」与 `base_url_unverified`);日志过滤;通义后缀检查;租户覆盖支持地址;所有调用处带地址、`ProviderKey` 与解析器改名、老方法失败即停、`fixed_provider_key_resolver` 绑定厂商;向量化按厂商取地址;两个指标与 span 属性;审计(含被拒的写入);ADR-0009 例外。

**本版新增 / 改写**(另:独立审查后的小修 —— 引用按解析后的名字比、两处 500 改成 422、回滚核对 SQL 不看启用状态、PR 2 表补齐):§0.1 威胁模型(后续审查的定级判据);规则「改地址必须同时给新密钥」(粘贴或换引用,清空回官方免填,§4.2);带自定义地址的密钥不进值缓存(§4.3);写入顺序写明「先密钥库、后凭证行」的理由;§4.10 发布与回滚(回滚前只打印行数的核对 SQL);§4.8 给 PR 2 的对外变化表按简单版重写;§6 写明 PR 1 已完成、PR 2 约 1 个工作日;§7.1 测试按本版需要的量重写(去掉槽位、回收、并发、加密的用例,加第 6、7、15 条的新内容);§8 A5 / A8 / A12 改写。

**早先各版(要点)**:

- 第 9 版(11-16):密钥加密后存进凭证行、AAD 绑主机 —— 已撤。
- 第 8 版(11-16):第三轮复审回修(删后不能靠引用复活、外部引用扫描、发布前三项检查)—— 已撤。
- 第 7 版(11-16):回收只由两步对账做;旧页面带旧引用 → 409;`key_id` 字符集;连通测试失败带接口名 —— 只保留「接口名」。
- 第 6 版(11-16):PR 1 实现后两路代码审查回修:并发与缓存下的不变式(槽位 + 比较后写 —— 已撤)、`azure` / `self-hosted` 必须粘贴(已撤);低优先级各项(等于官方根存空、规范化主机、IPv6 内嵌地址、解析超时、连通测试判据与意外不 500、被拒写入也记审计、§0 第 6 条更正)—— 保留。
- 第 5 版(11-16):路由切换规则本次不改,另立 B-309,如实写明现有行为。
- 第 4 版(11-16):改正「同厂商多把密钥遇到网络错误会互相顶上」的错误说法。
- 第 3 版(11-16):「必须当场粘贴密钥」(本版改为「改地址必须同时给新密钥」);写入顺序「检查 → 连通测试 → 写密钥库 → 写凭证行」(保留)。
- 第 2 版(11-16):六条拍板;解析器直接返回「引用 + 地址」、老方法失败即停、结构性测试;日志过滤;最底层选客户端。
- 第 1 版(11-16):初稿。
