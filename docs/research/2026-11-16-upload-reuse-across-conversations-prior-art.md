# 跨对话复用上传文件:业界做法调研(B-310)

日期:2026-11-16 · 只读调研,没改代码。

**读法**:标「推断」的是从文档或代码推出来的,标「未核实」的是没找到出处,都不能当事实用;本仓库的代码事实以设计稿 [B-310](../superpowers/specs/2026-11-16-b310-upload-limits-design.md) §5 里重新核对过的行号为准。

来源说明:本仓库的行号是调研作者读的。外部文档和开源仓库代码由三个子调研在 2026-11-16 读取(官方文档用无头浏览器读原文,开源仓库用 `gh api` 读指定提交),我没有逐条再核一遍。标「推断」的是从文档或代码推出来的,标「未核实」的是没找到出处。

## 1. 结论

- **业界通常只存一份、按 id 引用,不为每个对话复制。** 模型厂商的文件接口和我查的开源平台里,只有 FastGPT 把文件按对话存。
- **要把文件放进可写沙箱时,厂商才复制,而且复制件只读或不占配额。** Anthropic Managed Agents 写明会话里是「只读副本,不计入存储额度」;智谱同样复制成会话范围、只读;OpenAI 代码解释器复制进容器,容器按时长收费。
- **单次请求的文件个数和总字节上限几乎人人都有。** 所以方案 A(加单次总量上限)是通行做法。
- **阿里云 NAS 上没有「服务端复制」这条路。** 通用型 NAS 只支持 NFS v3 / v4.0,我们挂的是 v3,复制只能是客户端读一遍再写一遍。硬链接支持,单文件最多 511 个。
- **调研作者的建议**是现在先做 A(继续复制、加单次总量上限),终态做 B(只读引用)。**用户 11-16 的决定不同:直接做 B。** 设计见设计稿 §5。

## 2. 我们今天为什么复制

- **对话只看得到自己的目录。** B-262 P1 起,`uploads/` 就是 `threads/<对话>/work/` 下的普通子目录,别的对话的在视图里被盖住(`docs/superpowers/specs/2026-11-02-b262-p1-p3-conversation-platform-dirs.md:20-23`)。引用别的会话的上传时,文件必须出现在本对话的 `uploads/` 里,模型看到的路径才不变(同文件 54-63)。
- **智能体能改自己对话里的上传。** 复制前会比源文件大小,对不上回 409「上传后被改过」(`services/control-plane/src/control_plane/api/uploads.py:319-345`)。
- **留存按对话管。** 复制落地后在目标对话名下另建登记行,留存作业才管得到(`uploads.py:301-305, 359-371`)。
- 规格里没有写「为什么不用链接或只读挂载」,以上三条是从规格和代码读出来的(推断)。

今天这条路径在 200 MiB 下的问题(都是代码事实):

| 问题 | 位置 |
| --- | --- |
| 复制在「发起 run」的请求里逐个串行做,`files[]` 最多 64 条 | `api/agents.py:446-458`;`api/external_runs.py:124` |
| 整个源文件读进内存再写 | `uploads.py:327-332`(`read_file` 回 `bytes`) |
| 先读完才判断「目标里已有同名同内容」,重复引用也要整读一遍 | `uploads.py:327` 在前,`_land_and_account` 在 346 |
| 写入上限仍是 25 MiB | `services/orchestrator/src/orchestrator/tools/nas_workspace_store.py:238`(`_MAX_WRITE_BYTES`,`create_file` 在 1452 行检查) |
| 每次复制按整份字节再记一次配额 | `uploads.py:642-644`,记账在同一函数末尾 `note_written` |


## 3. 对比表

### 3.1 模型厂商

| 厂商 | 1 引用还是复制 | 2 配额怎么算 | 3 单次上限与报错 | 4 同步还是异步 | 5 按内容去重 |
| --- | --- | --- | --- | --- | --- |
| OpenAI | 文件上传一次,多处按 `file_id` 用;进代码解释器容器是复制;容器闲置 20 分钟作废,要重传 | 单文件 512 MB,每项目 2.5 TB;向量库按解析后大小另计;容器按时长收费。容器副本是否占 2.5 TB 未核实 | `input_file` 单次合计 50 MB;容器 `file_ids` 最多 50 个。超限的状态码未核实 | 容器复制在请求里做(推断);向量库挂载异步,有 `in_progress / completed / failed` | 文档没提 |
| Anthropic | Messages 按 `file_id` 引用;代码执行会把文件预载进容器;Managed Agents 会话里是只读副本,换一个新 id | 单文件 500 MB,每组织 1 TB;会话副本不占额度 | 请求 128 MB,PDF 600 页,每会话 500 个文件;413 文件过大,400 存储满,404 找不到 | 同步,文件对象没有处理状态字段(推断) | 文档没提;文件上传后不可改 |
| Gemini | 按文件 URI 跨请求引用 | 每项目 20 GB,单文件 2 GB,存 48 小时 | PDF 50 MB 或 1000 页;文件类错误码未核实 | 异步,有 `state`(枚举值只见搜索摘要) | 只暴露 sha256,没说去重 |
| Kimi | 老用法是客户端取回抽取文本放进消息;新的托管智能体按 `file_id` 绑会话,是否复制未核实 | 单文件 100 MiB,每组织 10 GiB | 超限回 400 `invalid_request_error` | 上传即返回 `ready / error` | 没有,只给同名加后缀 |
| 百炼 qwen-long | `fileid://` 引用,账号级 | 1 万个文件、100 GB,存储不收费 | 单次最多 100 个文件,单文件 150 MB,1.5 万页;400 `Too many files provided.` 等 | 异步;解析没完成时调用回「File parsing in progress」,只能重试 | 文档没提 |
| 智谱 | 对话按 `file_id`;托管智能体挂载时复制成会话范围的文件,只读 | 托管智能体单文件 500 MB、组织 500 GB;副本是否占额度未核实 | 对话单文件 50M;每会话 500 个;错误码未核实 | 上传同步;文档解析另有异步接口 | 没有 |

### 3.2 开源平台

| 系统 | 1 引用还是复制 | 2 配额与删除 | 3 单次上限与报错 | 4 同步还是异步 | 5 按内容去重 |
| --- | --- | --- | --- | --- | --- |
| Dify | 按 `upload_file_id` 引用,上传行不绑对话;校验租户和上传人 | 每次上传一个对象,没见引用计数 | 单文件 15 MB,每次 5 到 10 个,超了报「Number of files exceeds the maximum limit」;没有总字节上限 | 同步查表 | 存了哈希,上传时不用它去重 |
| LibreChat | 按 `file_id` 引用,有 `usage` 计数;代码环境自己留一份,过期重传 | 没见用户配额 | 10 个、单文件 512 MB、合计 512 MB;服务端超了**悄悄丢文件**,不报错 | 上传单独请求;代码环境在 run 开始时补传 | 没有 |
| LobeChat | 行指向按哈希存的共享对象 | **配额按行加总,一个对象被算 N 次**(4096 MB 记账对 3356 MB 实占,修复 PR 未合);删除按哈希数引用 | 10 个,单文件约 2 GB | 上传后异步切块 | 有,**全局跨用户**;查哈希接口不按用户过滤,会回文件元信息和地址(推断:可探测别人有没有某文件) |
| Open WebUI | 按 id 引用,`chat_file` 关联表 | 每次上传一个对象;删文件会从所有知识库一起删 | 默认不设上限;超大回 413 | 异步,`pending → completed / failed`,可轮询 | 哈希只用来挡同一向量集合里的重复内容 |
| Onyx | 按 id 引用,项目和助手用关联表 | 还有关联时拒绝删除;预算单位是 token | 单文件 100 MB;超限不让请求失败,在 `rejected` 列表里逐个给原因 | 异步,`PROCESSING`,可轮询 | 没有 |
| RAGFlow | 一个文件可链到多个知识库,共用存储位置 | 解除链接保留文件 | 未读 | 异步 | 没见 |
| AnythingLLM | 文档存一份,各工作区用行引用 | 每个工作区各嵌入一次(推断) | 未读 | 同步(推断) | 未读 |
| FastGPT | **按对话存**,存储键里带对话 id,换对话要重传(推断) | 按前缀清理,有过期机制 | 未读 | — | — |
| WeKnora | — | — | — | — | 有,范围是租户 + 知识库,重复时拒绝,不共享字节 |
| Coze | 未核实(只确认 SDK 有上传和查询接口) | | | | |

### 3.3 可写沙箱

| 产品 | 输入文件怎么进沙箱 | 能否只读 |
| --- | --- | --- |
| E2B | 逐沙箱写入(复制);快照可一份起多个沙箱;共享卷在内测,不支持只读挂载 | 否 |
| Daytona | 共享卷(对象存储上的 FUSE),可按子路径隔离,同路径后写者胜 | 没找到只读选项 |
| Modal | 共享卷和对象存储挂载,都可只读;文档建议多沙箱反复用的大数据走卷 | 是 |
| Claude Managed Agents | 平台为每个会话做只读副本,不计额度 | 是 |
| deer-flow(本地仓库) | 每个线程一个上传目录,读写挂进容器;分支线程整目录 `copytree` | 上传目录可写 |

没有哪家公开说用硬链接或 overlay 做这件事。Manus 只见搜索摘要,机制不公开。

## 4. 通行做法与例外

- **通行**:文件归用户或工作区所有,存一份,对话里只是一行引用;单次请求有个数和单文件上限。
- **进沙箱的两种做法**:平台复制一份只读的(Anthropic、智谱),或挂共享卷(Modal、Daytona)。
- **配额**:按上传的文件算一次。LobeChat 存储去重了、配额却按行算,被用户投诉。
- **同步还是异步**:轻的同步(查表、预载),重的异步带状态(解析、嵌入)。
- **去重**:很少有人做。全局去重只有 LobeChat,带跨用户探测风险。
- **例外**:FastGPT 按对话存;LibreChat 超限悄悄丢文件;百炼的状态字段不代表可用,只能靠重试。
- **我们特殊在哪**:上传落在智能体可写的对话目录里,所以才需要「每个对话一份」。别家要么不给沙箱写权限,要么给只读副本。

## 5. 四个方案放到我们这里

硬约束(已核实):

- NAS 用 NFS v3 挂载(`infra/k8s/base/control-plane/workspace-nas.yaml` 的 `vers: "3"`;生产补丁同样,且注明是通用型 NAS,`infra/k8s/overlays/prod/workspace-nas-patch.yaml:2,14`)。
- 沙箱里挂的是整个用户根,每次执行再用命名空间切成对话视图;沙箱进程 uid 10000,与文件属主相同(`services/orchestrator/src/orchestrator/tools/exec_view.py:3-12`;文件权限 0600 在 `nas_workspace_store.py:266`)。
- 执行视图已经有只读挂载的手法(`exec_view.py:254` 的 `bind_ro`,365 行用于 `shared/`)。
- 配额重算是把用户根下每个文件的大小加总,不按 inode 去重(`services/control-plane/src/control_plane/workspace_quota.py:133-159`)。

### A. 继续复制,加单次总量上限

- **改动**:复制前用登记行的大小先算总量,超了整单拒绝并给明确错误码;配额也按总量先查一次。
- **代价**:小,只动控制面;对外多一个错误码。
- **风险**:上限定多少要实测。通用型 NAS 容量型起步吞吐 150 MB/s,64 个 200 MiB 约 90 到 180 秒(子调研按文档公式算的,推断),贴着 180 秒网关超时(仓库里的出处:`infra/k8s/cluster/albconfig-listeners-patch.yaml`;11-16 实测这 180 秒不计客户端上传时间,见设计稿 §2.3)。配额仍然每引用一次算一次。

### B. 只读引用,不复制,配额算一次

- **做法**:每份上传在用户级的存储目录里放一份,对话视图够不着;对话的 `uploads/` 里放硬链接;执行视图把它只读挂上。
- **硬链接本身不安全**:同一个 inode,沙箱进程又是属主,原地写会改掉源和所有别的对话。必须配只读挂载。
- **代价**:
  - 两份执行脚本都要改,要新的沙箱布局版本。
  - 宿主侧 `write_file` / `edit_file` 要拒绝写这些文件。
  - 配额重算改成按 inode 去重,复制路径的增量记账跳过。
  - 留存改成「删名字」,最后一个名字删掉才释放空间(文件系统自带的引用计数)。
- **风险**:
  - 智能体今天能写 `uploads/`,改成只读是行为变化;有没有智能体依赖这点,没数过。
  - 单文件最多 511 个硬链接,一份文件挂到更多对话时要再存一份。
  - NAS 账单和目录配额怎么算硬链接,文档没写。
  - 跨目录硬链接在通用型 NAS 上没有成文限制,需真机试;`create_file` 已在同目录用 `link`(`nas_workspace_store.py:1471`)。
- **对外契约**:`upl_` id 不变;附件名不再需要 `-<sha8>` 以外的变化(推断)。

### C. 复制挪到 run 里做

- **做法**:请求里只做校验和总量、配额预检,立即返回;复制放到 run 开头的节点,和开场复制同一位置(`conversation_seed`,见 B-236 规格);复制完再调模型。
- **代价**:中等。今天请求里同步回的 404 / 409 / 429 有一部分变成 run 失败事件,对接方要改处理;失败时已建会话的回撤(B-293)要重做。
- **风险**:配额和内存问题没解决,只是不再撞网关超时。
- **先例**:Gemini、百炼、Open WebUI、Onyx 都是异步带状态;百炼「只能重试」的做法不要学。

### D. 写时复制 / 服务端复制

- **走不通**。通用型 NAS「仅支持 NFS v3 和 NFS v4.0 协议」(https://help.aliyun.com/zh/nas/product-overview/limits );服务端复制和克隆是 NFS v4.2 才有的可选操作(RFC 7862 §4)。阿里云没有一句话明说「不支持」,这是由版本推出来的(推断)。
- 没找到在文件系统内部复制单个文件的 OpenAPI。
- overlay 也不行:上层目录不能放在 NFS 上(https://docs.kernel.org/filesystems/overlayfs.html )。

## 6. 调研作者的建议与用户的决定

- 调研作者的建议:现在做 A(单次复制总量上限 + 已有同名同内容时不读源文件 + 复制改流式),终态做 B;C 作备选;不做跨用户按内容去重。
- **用户 11-16 定:在 B-310 里直接做 B(只读引用),不走「继续复制 + 总量上限」。** 设计稿 §5 按此写;与本节建议不同之处以设计稿为准。
- B 开工前的前置实测(数 `uploads/` 下的写入、真机试跨目录硬链接与只读挂载、看 NAS 怎么给硬链接算用量、定 511 上限的兜底)已写成设计稿 §5.10 的步骤 0。

## 7. 没能核实的

- 180 秒网关超时的出处;NAS 实际复制速度(只有文档公式,没实测;内地地域的吞吐上限表没读到)。
- NAS 账单和目录配额对硬链接的算法;通用型 NAS 跨目录硬链接有无限制。
- OpenAI:超 50 MB / 512 MB / 2.5 TB 的错误码;容器副本是否占项目额度。
- Anthropic:单请求 `container_upload` 个数上限;代码执行容器副本是否占 1 TB。
- Gemini 状态枚举(只见摘要);Kimi 托管智能体绑定是否复制;智谱会话副本是否占额度。
- Coze 全部(除接口路径);Manus 机制;Dify 代码节点是否拿到文件字节;LibreChat「我的文件」复用路径本体;Onyx 文件处理中时发消息的行为。
- 开源仓库里的否定结论(「没有去重」「没有总字节上限」)只基于读过的那几个文件,不是穷尽搜索。

## 8. 主要出处

- OpenAI:https://platform.openai.com/docs/api-reference/files/create · https://platform.openai.com/docs/guides/tools-code-interpreter · https://platform.openai.com/docs/guides/pdf-files · https://platform.openai.com/docs/guides/retrieval
- Anthropic:https://platform.claude.com/docs/en/build-with-claude/files · https://platform.claude.com/docs/en/managed-agents/files · https://platform.claude.com/docs/en/agents-and-tools/tool-use/code-execution-tool · https://platform.claude.com/docs/en/build-with-claude/pdf-support
- Gemini:https://ai.google.dev/gemini-api/docs/files · https://ai.google.dev/api/files
- Kimi:https://platform.moonshot.cn/docs/api/files · https://platform.kimi.com/docs/hosted-agents/files.md
- 百炼:https://help.aliyun.com/zh/model-studio/long-context-qwen-long · https://help.aliyun.com/zh/model-studio/openai-file-interface · https://help.aliyun.com/zh/model-studio/error-code
- 智谱:https://docs.bigmodel.cn/cn/managed-agents/files.md
- 阿里云 NAS:https://help.aliyun.com/zh/nas/product-overview/limits · https://help.aliyun.com/zh/nas/product-overview/general-purpose-nas-file-systems
- 沙箱:https://docs.e2b.dev/faq/volumes-beta-limitations.md · https://www.daytona.io/docs/en/volumes/ · https://modal.com/docs/guide/volumes
- 内核与协议:https://docs.kernel.org/filesystems/overlayfs.html · https://man7.org/linux/man-pages/man2/link.2.html · https://www.rfc-editor.org/rfc/rfc7862.txt
- 开源仓库(提交号):
  - Dify `3f72db6c`:`api/factories/file_factory/builders.py:156-237`、`api/configs/feature/__init__.py:508-600`
  - LibreChat `9363db54`:`packages/data-provider/src/file-config.ts:555-597`、`packages/api/src/files/filter.ts:75-108`
  - LobeChat `e18e7142`:`packages/database/src/models/file.ts:177-250, 380-390`;PR lobehub/lobehub#19522
  - Open WebUI `b0e6098e`:`backend/open_webui/routers/files.py`
  - Onyx `3cd29fc0`:`backend/onyx/server/features/projects/api.py:280-625`
  - FastGPT `8cee7a16`:`packages/service/common/s3/sources/chat/key.ts`
  - deer-flow 本地 `cd34a1a5`:`backend/app/gateway/routers/threads.py:187-195`
