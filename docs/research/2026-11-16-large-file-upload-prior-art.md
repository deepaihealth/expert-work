# 大文件上传与读取上限:业界做法调研(B-310)

日期:2026-11-16。只读调研,没有改任何代码。**网页上的数字全部是搜索引擎返回的摘要,没有打开原页面核对,下文标「摘要」;用之前请打开链接确认。** 本地仓库的数字读自代码,带文件与行号。

## 0. 结论先说

1. **「字节上限 + 按内容的第二道上限」是所有大厂的共同做法**,我们缺的正是第二道。Claude:500 MB 且 PDF 超 1000 页拒收;ChatGPT:512 MB 且文本类 200 万 token;NotebookLM:200 MB 或 50 万词;百炼知识库:150 MB 且 1000 页。
2. **200 MB 量级经应用服务器上传是常见做法**,不必须直传对象存储。LibreChat 默认 512 MB、RAGFlow 1 GiB、Onyx 250 MB 都走自己的后端。直传 / 分片的是上限到 GB 级、且文件本来就存对象存储的产品(LobeChat 2 GB、FastGPT 1000 MB、OpenAI Uploads API 8 GB)。
3. **走后端的那几家,靠的是把代理超时放到 3600 秒**(Dify、RAGFlow 的 nginx)。我们的 180 秒是真正的差异点。阿里云文档显示这个值可以调高(见 §1.3),这一条比改架构便宜得多。
4. **没有一家用 tus**(本地 9 个仓库搜索零命中)。需要续传的都是自己做「建会话 → 传分片 → 合并」。
5. 设计稿五个 PR 的顺序与常见做法一致。如果网关实测确实卡 180 秒且调不高,下一步应是**经应用服务器的分片上传**(FastGPT 的做法),不是预签名直传 —— 因为我们的文档在 NAS 上,NAS 没有预签名地址。

## 来源说明

- 本地仓库根目录 `/Users/mac/src/github/`,下文路径都相对它。读到的版本:`ai/FastGPT` 3fa17bea0(11-10)、`ai/LibreChat` f4154b649(10-31)、`ai/lobe-chat-upstream` f94b8f3e(11-10)、`ai/onyx` da837e6(11-10)、`ai/ragflow` 3299ed0(11-10)、`ai/MaxKB` 2900bd8(11-10)、`WeKnora` cbdcebabf(11-09)、`openclaw` 017a4ede84c(10-20)、`hermes-agent` 40cc180823(10-20)、`deer-flow` cd34a1a5(07-20)、`ai/dify/dify` f6305858a5(**2025-04-26,很旧**,Dify 的数字只代表那个版本)。
- 本地**没有** Open WebUI、AnythingLLM、Coze 的仓库,这三家只有网页信息。
- 网页信息全部来自搜索引擎返回的摘要,**我没能打开原页面**(curl 被权限拒绝)。凡标「摘要」的数字,用之前请打开链接核对。同一页面不同缓存版本数字互相打架的地方,我都写明了。

## 1. 大文件怎么传上来

### 1.1 开源项目

| 项目 | 路径 | 怎么处理字节 | 默认上限 | 出处 |
|---|---|---|---|---|
| Dify(2025-04 版) | 经后端 | `file.read()` 整份进内存再存 | 文档 15 MB、图片 10、音频 50、视频 100 | `ai/dify/dify/api/controllers/console/files.py:74`;`api/configs/feature/__init__.py:251-272` |
| LibreChat | 经后端 | multer 落磁盘,不进内存 | 512 MB(服务端与各端点同值) | `ai/LibreChat/api/server/routes/files/multer.js:17`、`:144`;`packages/data-provider/src/file-config.ts:555`、`:596` |
| deer-flow | 经后端 | 8 KB 一块边读边写临时文件,累计超限即 413,成功才提交、失败清理 | 单份 50 MB、每次 10 份、合计 100 MB | `deer-flow/backend/app/gateway/routers/uploads.py:42-46`、`:252-279` |
| RAGFlow | 经 nginx + 后端 | 普通 multipart | 请求体 1 GiB;知识库文档 128 MiB | `ai/ragflow/docs/faq.mdx:327-329`;`docker/nginx/nginx.conf:29` |
| Onyx | 经后端(文件存储层另有预签名上传的实现) | FastAPI `UploadFile` | 用户文件 100 MB,管理员可调,硬顶 250 MB | `ai/onyx/backend/onyx/configs/app_configs.py:63-70`;预签名:`backend/onyx/file_store/file_store.py:495`、`app_configs.py:2139` |
| WeKnora | 经 nginx + 后端 | 普通 multipart;对话附件也可以 base64 塞在聊天请求里 | 50 MB;对话每次 5 份、合计 100 MB | `WeKnora/.env.example:211`;`frontend/nginx.conf:19-20`;`internal/handler/session/qa.go:38-39` |
| LobeChat | **浏览器直传对象存储**(预签名) | 小于 64 MB 单次 PUT;≥ 64 MB 分片,每片 32 MB,最多 1 万片;失败时调 abort 清理 | 2 GB − 1 字节,每次 10 份 | `ai/lobe-chat-upstream/src/services/upload.ts:15-17`、`:172-183`、`:303-350`;`packages/const/src/file.ts:20-22` |
| FastGPT | **分片上传,经自己的代理层**到对象存储 | ≥ 32 MB 走分片:每片 8 MB、3 路并发、每片最多重试 3 次、会话 3 小时过期、可取消 | 1000 MB(系统级,可配;团队套餐可再压低) | `ai/FastGPT/packages/service/common/s3/config/constants.ts:16-28`;`packages/web/common/file/uploader/index.ts:5-20`;`packages/service/env.ts:488` |

FastGPT「经代理层」是我从注释原文「浏览器到 FastGPT 对象存储代理」「切换到代理层 Multipart 上传」和类型名 `S3ProxyUploadPayload` 读出来的(`packages/service/common/s3/accessLink/uploadSession/service.ts:19`),**没有逐行跟到网络请求,算推断**。

Onyx 的预签名上传有没有接到对话上传的接口上,我没有查到(在 `backend/onyx/server` 下搜 presign 无命中),只能说存储层有这个能力。

### 1.2 厂商接口(网页摘要)

| 接口 | 做法 | 数字 | 来源 |
|---|---|---|---|
| OpenAI Uploads API | 建会话 → 传多个分片(可并行)→ 合并成文件;可取消 | 总量最多 8 GB,会话建立后 1 小时过期 | https://developers.openai.com/api/reference/resources/uploads/methods/create |
| OpenAI Files / 向量库 | 单次上传 | 单文件 512 MB、500 万 token | https://developers.openai.com/api/docs/guides/retrieval |
| Anthropic Files API | 单次 multipart 上传 | 单文件 500 MB;组织总量摘要里有 1 TB 和 100 GB 两个版本 | https://platform.claude.com/docs/en/build-with-claude/files |
| Gemini Files API | 可续传协议 | 单文件 2 GB、每项目 20 GB、保留 48 小时 | https://ai.google.dev/gemini-api/docs/file-input-methods |
| Kimi(Moonshot) | 单次上传 | 单文件 100 MiB | https://platform.kimi.com/docs/api/files-upload.md |
| 百炼文件接口 | 单次上传 | 文档抽取用途 150 MB;总量 1 万个 / 100 GB | https://help.aliyun.com/zh/model-studio/upload-file-api 、https://help.aliyun.com/zh/dashscope/developer-reference/openai-file-interface |

「OpenAI 分片 64 MB」只在第三方镜像里出现,是示例代码里客户端自己选的值,不是官方上限,**未证实**。

### 1.3 网关超时怎么躲

| 做法 | 谁在用 | 出处 |
|---|---|---|
| 把代理超时放到 3600 秒 | Dify(`NGINX_PROXY_READ_TIMEOUT=3600s`,gunicorn 360 秒)、RAGFlow(`proxy_read_timeout 3600s`) | Dify 的 `docker/.env.example:143`、`:933-934`(只看了搜索命中的行);`ai/ragflow/docker/nginx/proxy.conf:7` |
| 切成小请求,每个都远短于超时 | FastGPT(8 MB 一片)、LobeChat(32 MB 一片)、OpenAI Uploads API | 见 1.1、1.2 |
| 字节不经过自己的网关 | LobeChat(直传对象存储) | 见 1.1 |

**关于我们的 ALB(摘要,对本设计很关键,请核对原页面):**

- ALB 自身的请求体上限是 **50 GB**,超过才由 ALB 自己回 413。所以 200 MiB 不会被 ALB 按大小拦。来源:https://help.aliyun.com/zh/slb/application-load-balancer/support/alb-exception-status-code-description
- `RequestTimeout` 的文档描述是「后端在超时时间内未响应,ALB 停止等待并回 504」。照字面,计的是**等后端响应**的时间,不是客户端上传的时间。**这是我对文档措辞的推断,不能代替设计稿第 0 步的实测。**
- 取值范围各文档不一致:CreateListener 接口文档写 1~600 秒;ROS 和 ACS 的 AlbConfig 文档写 1~180;另有说明可通过配额中心申请到 900 秒(升级实例 3600 秒)。我们仓库头注说的「180 是不提工单的上限」与 ACS 文档一致。来源:https://help.aliyun.com/zh/slb/application-load-balancer/developer-reference/api-alb-2020-06-16-createlistener 、https://www.alibabacloud.com/help/en/cs/user-guide/configure-the-alb-listener-through-the-albconfig 、https://help.aliyun.com/en/slb/application-load-balancer/create-and-manage-listeners

### 1.4 进度、取消、续传

- 进度与取消:LobeChat、FastGPT、LibreChat 都有(LibreChat 的取消文案还提醒「上传可能仍在处理,需手动删除」,`ai/LibreChat/client/src/locales/en/translation.json:678`)。
- 分片失败自动重试:只有 FastGPT(每片 3 次)。
- 关掉页面后接着传(真正的断点续传):**本地读到的项目里没有一家做**。LobeChat 失败即 abort 整个上传。Gemini Files API 的协议支持续传。
- tus:9 个本地仓库搜 `tus-js-client|@uppy/tus|tusd|Upload-Offset|Tus-Resumable` 全部零命中。

## 2. 字节没超、但内容太大读不完时怎么办

| 产品 | 做法 | 告诉用户 / 模型什么 | 出处 |
|---|---|---|---|
| Claude 对话 | 100 页以内:文字 + 图表都看;101~1000 页:**只读文字**;超过 1000 页:**拒收** | 超限报「Uploaded file is too large」 | https://support.claude.com/en/articles/8241126-upload-files-to-claude (摘要) |
| Claude 项目 | 知识量接近上下文上限时**自动切换成检索**,容量约 10 倍;量降下来自动切回 | 界面有「已启用检索」的标记 | https://support.claude.com/en/articles/11473015-retrieval-augmented-generation-rag-for-projects (摘要) |
| ChatGPT | 抽出文字后,**一部分直接放进上下文,其余放进检索索引**;企业版合计 11 万 token 以内全文放入;文本类超 200 万 token 拒收 | 摘要里没看到对用户的提示 | https://help.openai.com/en/articles/10029836-optimizing-file-uploads-in-chatgpt-enterprise 、https://help.openai.com/en/articles/8555545-file-uploads-faq (摘要) |
| Onyx | 上传时就抽文字数 token,超阈值**拒收**(有向量库时 20 万 token);表格类例外:收下但不索引,只把文件信息告诉模型 | 拒收原因「Exceeds 200K token limit」 | `ai/onyx/backend/onyx/server/features/projects/projects_file_utils.py:354-374`;`backend/onyx/server/settings/models.py:20-21` |
| AnythingLLM | 附件默认全文放入;超过上下文时**弹窗让用户选**:取消 / 改成入库检索 / 继续(会被裁剪,文档明说不推荐) | 输入框上显示上下文占用 | https://docs.anythingllm.com/chatting-with-documents/introduction (摘要) |
| LibreChat | 全文截到 10 万 token | **只打日志,不告诉模型也不告诉用户** | `ai/LibreChat/packages/api/src/files/context.ts:75-86`;`packages/api/src/utils/text.ts:91-98`;默认值 `packages/data-provider/src/file-config.ts:557` |
| openclaw | PDF 只取前 N 页(输入附件 4 页;pdf 工具 20 页、10 MB),文字截到 6 万字符 | 没核对提示文案 | `openclaw/src/media/input-files.ts:91-95`;`openclaw/src/agents/tools/pdf-tool.ts:72-73` |
| LobeChat | 能传到 2 GB,但超过 64 MB 的不在内存里解析(我只看到这个上限用在解压 zip 的地方,其它格式没查) | 报「cannot be parsed in memory」 | `ai/lobe-chat-upstream/packages/const/src/file.ts:26-28`;`src/utils/unzipFile.ts:83` |
| Open WebUI | 每个附件上传时都先切块入向量库(即使选了「用整份文档」) | — | https://github.com/open-webui/open-webui/issues/32295 (摘要,社区帖) |
| NotebookLM | 每个来源 50 万词或 200 MB,没有页数限制;全部走检索 | — | https://support.google.com/notebooklm/answer/16269187 (摘要) |

**归纳:** 三种做法都有人用。

- 拒收:Claude(超 1000 页)、ChatGPT(超 200 万 token)、Onyx(超 20 万 token)。都是在**很高**的位置设一条硬线。
- 收下 + 降级读:Claude(101~1000 页只读文字)、openclaw(只读前 N 页)、LibreChat(截断)。
- 收下 + 转检索:ChatGPT、Claude 项目、AnythingLLM(让用户选)、Open WebUI、NotebookLM。

**没人做的:** 「异步处理 + 状态」只出现在知识库入库路径,对话附件里没有看到谁让用户等一个后台任务。LibreChat 的「悄悄截断不说」是反面例子。

## 3. 字节之外的第二道上限

| 产品 | 页数 | token / 词 | 表格 | 个数 | 总量 | 出处 |
|---|---|---|---|---|---|---|
| ChatGPT | — | 文本类 200 万 token | 约 50 MB | 每 3 小时 80 个;免费版每天 3 个;项目 20 / 40 个 | 摘要里是每用户 25 GB、每组织 100 GB(页面有多个缓存版本,数字不一) | https://help.openai.com/en/articles/8555545-file-uploads-faq (摘要) |
| Claude 对话 | PDF 1000 页 | 受上下文限制 | — | 每个对话 20 个 | — | https://support.claude.com/en/articles/8241126-upload-files-to-claude (摘要) |
| Claude API | 每次请求 600 页(上下文不到 100 万 token 的模型 100 页) | — | — | — | 请求体 32 MB(另一个版本写 128 MB) | https://platform.claude.com/docs/en/build-with-claude/pdf-support (摘要) |
| Gemini 应用 | — | — | — | 每次 10 个 | 视频 2 GB,其它 100 MB | https://support.google.com/gemini/answer/14903178 (摘要) |
| Gemini API | PDF 1000 页 / 50 MB(旧模型);新模型云文档写 3000 页 | — | — | — | — | https://ai.google.dev/gemini-api/docs/document-processing (摘要) |
| 百炼知识库 | PDF / Word / PPT:150 MB 且 1000 页 | — | Excel 10 MB 且 10 万行 | 控制台每次 50 个;每空间 10 万个 | — | https://help.aliyun.com/zh/model-studio/rag-knowledge-base-specifications (摘要) |
| OpenAI 向量库 | — | 每文件 500 万 token | — | — | 首 1 GB 免费,之后按天计费 | https://developers.openai.com/api/docs/guides/retrieval (摘要) |
| NotebookLM | 无 | 50 万词 | — | 每本 50 个起,按套餐 100 / 300 / 500 / 600 | — | https://support.google.com/notebooklm/answer/16213268 (摘要) |
| Onyx | — | 20 万 token(表格类豁免) | — | — | 每个文件内嵌图片 500 张,每批 1000 张 | `ai/onyx/backend/onyx/configs/app_configs.py:1629-1647`;`projects_file_utils.py:92-94` |
| LibreChat | — | 10 万 token | — | 每次 10 个,合计 512 MB | 抽出的文字最多存 15 MiB | `ai/LibreChat/packages/data-provider/src/file-config.ts:557-564`;`packages/api/src/files/extract.ts:15` |
| deer-flow | — | — | — | 每次 10 个,合计 100 MB | — | `deer-flow/backend/app/gateway/routers/uploads.py:43-46` |
| WeKnora | — | — | — | 每次 5 个,合计 100 MB | — | `WeKnora/internal/handler/session/qa.go:38-39` |
| Dify(旧版) | — | — | — | 每批 5 个;工作流 10 个 | 云版按套餐有文档上传配额 | `ai/dify/dify/api/configs/feature/__init__.py:256`、`:281`;`api/services/dataset_service.py:1159-1160` |

**归纳:**

- 页数上限集中在 **1000 页**(Claude、Gemini 旧模型、百炼)。
- token 上限从 10 万(LibreChat,截断)到 200 万(ChatGPT,拒收)不等,取决于超了之后是截断还是转检索。
- **表格单独一个更小的上限**是共同点:ChatGPT 约 50 MB、百炼 10 MB / 10 万行;Onyx 把表格从 token 上限里豁免但不索引。
- Onyx 的「内嵌图片张数」上限是为了防止后台解析被撑爆内存(`projects_file_utils.py:269-275` 的注释原文),和我们设计稿 §2.7 担心的是同一类问题。

## 4. 图片

### 4.1 产品怎么处理

| 项目 | 上传上限 | 送模型前怎么处理 | 在哪做 | 出处 |
|---|---|---|---|---|
| LibreChat | 同文件(512 MB) | 可选的浏览器端缩到 1900×1900、质量 0.92(默认关);服务端再缩:low 512×512,high 768×2000 | 两端 | `ai/LibreChat/packages/data-provider/src/file-config.ts:601-605`;`api/server/services/Files/images/resize.js:10-11` |
| LobeChat | — | 长边超 1920 或大于 3 MB 就在浏览器里压 | 浏览器 | `ai/lobe-chat-upstream/packages/utils/src/compressImage.ts:3-7`、`:88-90` |
| hermes-agent | 下载上限 50 MB;合计 1 亿像素 | 长边缩到 1568;被厂商按大小拒了再压到 5 MB 重试一次 | 服务端 | `hermes-agent/tools/vision_tools.py:90`、`:365-368`;`tools/vision_tools_image_prep.py:248` |
| openclaw | 10 MB | 长边 1200、5 MB | 服务端 | `openclaw/src/media/input-files.ts:90`;`openclaw/src/agents/image-sanitization.ts:9-10` |
| WeKnora | 10 MB(嵌入页另一处 20 MB) | 没查 | 浏览器预检 | `WeKnora/frontend/src/components/Input-field.vue:144`;`EmbedInputField.vue:179`、`:223` |
| Dify(旧版) | 10 MB | 没查 | — | `ai/dify/dify/api/configs/feature/__init__.py:261-264` |
| Onyx | 同文件 | 先按图估 token,超 token 阈值拒收 | 服务端 | `ai/onyx/backend/onyx/server/features/projects/projects_file_utils.py:226-248` |

### 4.2 厂商接口的单图上限(网页摘要)

| 厂商 | 单图大小 | 像素 | 其它 | 来源 |
|---|---|---|---|---|
| OpenAI 接口 | 没有查到单图数字 | 高细节档最长边 2048,自动缩小 | 每次请求总量 50 MB / 500 张(旧版);新版页面写 512 MB | https://developers.openai.com/api/docs/guides/images-vision (摘要,新旧版冲突) |
| ChatGPT 应用 | 20 MB | — | — | https://help.openai.com/en/articles/8555545-file-uploads-faq (摘要) |
| Claude 接口 | 5 MB(旧版文档,以及 Bedrock / Google 云上);另一版本写直连 10 MB | 8000×8000;一次超过 20 张时每张不超过 2000 | 每次请求 100 张(20 万上下文的模型)或 600 张 | https://platform.claude.com/docs/en/build-with-claude/vision (摘要,版本间冲突) |
| Claude 对话 | — | 8000×8000 | — | https://support.claude.com/en/articles/8241126-upload-files-to-claude (摘要) |
| Gemini 接口 | 内联请求合计 100 MB(由 20 MB 提上来);新模型云文档写每段 256 MB | — | 每次最多 3000 张(新模型) | https://blog.google/innovation-and-ai/technology/developers-tools/gemini-api-new-file-limits/ (摘要) |
| 通义千问视觉 | 公网地址:新系列 20 MB,其它 10 MB;本地与 base64:10 MB | 宽高都不小于 10 像素,宽高比不超过 200:1 | 请求体 64 MB | https://help.aliyun.com/zh/model-studio/vision (摘要) |
| 智谱 GLM 视觉 | 5 MB | 6000×6000 | — | 只有第三方文章 https://juejin.cn/post/7448881249002143782 (2024 年底),**官方页没搜到** |
| 豆包(方舟) | **没搜到** | **没搜到** | — | — |
| Kimi | **没搜到单图上限** | — | 文件接口 100 MiB | https://platform.kimi.com/docs/api/files-upload.md |

**归纳:** 送进模型的图,各家都缩到长边 1200~2000 像素;厂商接口的单图上限在 5~20 MB 之间。上传上限 10 MB 是开源项目里最常见的值(Dify、openclaw、WeKnora)。用户传多大和模型实际看到多大是两回事,中间那一步缩图各家都有。

## 5. 知识库上限与对话附件上限

| 产品 | 对话附件 | 知识库 | 是否同一个数 | 出处 |
|---|---|---|---|---|
| Dify(旧版) | 15 MB | 15 MB | **同一个配置**,同一个上传接口 | `ai/dify/dify/api/controllers/console/files.py:38`;`web/app/components/datasets/create/file-uploader/index.tsx:52`、`:73` |
| RAGFlow | 文件管理 1 GiB | 知识库文档 128 MiB;文档信息上传 100 MiB(调大总上限也不跟着变) | **分开** | `ai/ragflow/docs/faq.mdx:327-329` |
| Onyx | 用户文件 100 MB(硬顶 250) | 连接器文件 2 GB、字符数 5.36 亿 | **分开** | `ai/onyx/backend/onyx/configs/app_configs.py:63-70`、`:1572`、`:1614-1616` |
| WeKnora | 跟随同一个 50 MB,另加每次 5 份 / 合计 100 MB | 50 MB | 单份同值,对话多一道合计上限 | `WeKnora/internal/handler/knowledge.go:331`;`internal/handler/session/temporary_document.go:48`;`internal/handler/session/qa.go:38-39` |
| MaxKB | 没查 | **每个知识库自己设**,默认 100 MB、50 个 | — | `ai/MaxKB/apps/knowledge/models/knowledge.py:140-141`;`apps/knowledge/serializers/document.py:1219-1222` |
| FastGPT | 系统级 1000 MB,团队套餐可再压低 | 共用同一套预签名上传与系统上限(没逐条核对知识库入口) | 大体同一个数 | `ai/FastGPT/packages/service/core/workflow/utils/fileLimits.ts:39-55`;`packages/service/common/s3/buckets/base.ts:357` |
| Open WebUI | `RAG_FILE_MAX_SIZE` 默认不限;对话个数 `RAG_FILE_MAX_COUNT` | 个数另有 `KNOWLEDGE_MAX_UPLOAD_COUNT` | 大小同一个,个数分开 | https://docs.openwebui.com/reference/env-configuration/ (摘要) |
| ChatGPT / OpenAI | 512 MB、200 万 token | 向量库 512 MB、500 万 token | 字节同,token 不同 | 见 §3 |
| Claude | 对话 500 MB | 项目 30 MB | **分开,知识库反而小** | https://support.claude.com/en/articles/8241126-upload-files-to-claude (摘要) |
| 百炼 | 文件接口 150 MB | 150 MB / 1000 页;纯文本类 10 MB;Excel 10 MB / 10 万行;图片 20 MB | 字节同,知识库多了按类型的内容上限 | 见 §1.2、§3 |
| NotebookLM | — | 200 MB 或 50 万词 | — | 见 §3 |
| Coze | **没搜到官方数字** | **没搜到官方数字** | — | — |
| AnythingLLM | **没搜到上限数字** | **没搜到上限数字** | — | — |

**归纳:** 多数分开,或者字节相同但知识库多一层按内容、按类型的上限。为什么分开,只有两处有原文依据:Onyx 的注释说内嵌图片太多会把后台解析进程撑爆(`projects_file_utils.py:269-275`);OpenAI 向量库按存储量按天收费。其余「因为入库要花嵌入的钱、走异步管线」是我的推断。Claude 项目 30 MB 比对话 500 MB 小得多,说明**知识库上限不必跟着对话上限走**。

## 6. 「太大了」怎么告诉用户

| 项目 | 浏览器先拦 | 上限从哪来 | 文案带具体数字 | 本地化 | 出处 |
|---|---|---|---|---|---|
| Dify | 是 | 接口 `GET /files/upload` 返回各类上限,前端取用 | 是 | 是 | `ai/dify/dify/api/controllers/console/files.py:36-43`;`web/app/components/datasets/create/file-uploader/index.tsx:102-104`、`:277`;后端 413 + `file_too_large`:`api/controllers/web/error.py:112-115` |
| LibreChat | 是 | 服务端配置下发,按端点区分 | 是:「文件过大,请上传小于 {{0}} MB 的文件」;一批里部分超限时点名是哪几个文件 | 是 | `ai/LibreChat/client/src/locales/zh-Hans/translation.json:384`;`client/src/hooks/Files/useFileHandling.ts:264-286` |
| WeKnora | 是 | 构建期变量 | 是:「文件大小不能超过 {size}M!」;图片文案把格式和大小一起写 | 是 | `WeKnora/frontend/src/components/AttachmentUpload.vue:106-107`;`frontend/src/i18n/locales/zh-CN.ts:4417`、`:5381` |
| LobeChat | 是 | 常量 | 带字节数,但是英文硬编码(「Files larger than 2147483647 bytes…」) | 否 | `ai/lobe-chat-upstream/src/services/upload.ts:77`;`packages/const/src/file.ts:24` |
| Onyx | 否(服务端判) | 管理员设置 | 是;每个被拒文件各带一条原因,其余文件照常上传 | 英文 | `ai/onyx/backend/onyx/server/features/projects/projects_file_utils.py:212-221` |
| deer-flow | 接口把上限暴露给客户端 | 配置 | 只有「File too large: 文件名」,不带数字 | 英文 | `deer-flow/backend/app/gateway/routers/uploads.py:89-93`、`:269` |
| MaxKB | — | 每个知识库的设置 | 是:「上传文件不能超过 {} MB」 | 是 | `ai/MaxKB/apps/knowledge/serializers/document.py:1222` |

**归纳:** 「后端有个接口返回上限 → 前端先拦 → 文案带具体数字 → 本地化」是 Dify、LibreChat、WeKnora 的共同做法,和我们 PR 1 的设计一致。值得抄的一点:一次选多个文件时,合格的照传,不合格的逐个点名(LibreChat、Onyx)。

## 7. 分档与可配置

| 做法 | 谁在用 | 出处 |
|---|---|---|
| 按套餐分档 | FastGPT(团队套餐的单文件大小和个数,取套餐与系统上限里小的);Dify 云(文档上传配额,免费档每次只能传 1 个);NotebookLM(每本 50 / 100 / 300 / 500 / 600 个来源);ChatGPT(免费每天 3 个);Claude 项目检索(仅付费) | `ai/FastGPT/packages/service/core/workflow/utils/fileLimits.ts:39-55`;`ai/dify/dify/api/services/dataset_service.py:893-914`;其余见 §2、§3 |
| 管理员可调 + 平台硬顶 | Onyx:默认 100 MB,管理员在设置里改,不能超过 250 MB 硬顶,超了自动压回 | `ai/onyx/backend/onyx/configs/app_configs.py:61-70`;`backend/onyx/server/settings/store.py:110-123` |
| 按端点 / 按知识库可调 | LibreChat(每个端点各自的个数、单份、合计上限);MaxKB(每个知识库) | `ai/LibreChat/packages/data-provider/src/file-config.ts:647-653`;`ai/MaxKB/apps/knowledge/models/knowledge.py:140-141` |
| 只有一个环境变量 | Dify 自部署、WeKnora、RAGFlow、deer-flow、Open WebUI | 见前文 |
| 同一产品不同入口用不同上限 | LobeChat:自己人 2 GB,分享页访客 32 MB,理由写明是访客的字节落在主人的存储上、且 32 MB 低于分片阈值、不用处理分片会话 | `ai/lobe-chat-upstream/packages/const/src/agentShare.ts:74-79` |

**没看到的:** 没有哪家公开地「先放一个中间档,过段时间再提」。能起到同样作用的是「默认值 + 可调 + 硬顶」。

## 8. 这些做法怎么用到 B-310

见设计稿 [B-310](../superpowers/specs/2026-11-16-b310-upload-limits-design.md) §3、§4、§9。用户 11-16 已定:字节没超但内容太大的文档照收、部分读、明说;图片与知识库上限不变;不设中间档(网关超时卡住且调不高时先停 100 MiB);网关卡住时的下一步是经应用服务器的分片上传,不是直传存储。

## 9. 没能核实的

1. **所有网页数字都是搜索摘要**,没打开原页面。互相打架的:Claude 接口单图 5 MB 还是 10 MB、请求体 32 MB 还是 128 MB;OpenAI 接口每次请求 50 MB 还是 512 MB;Anthropic Files 组织总量 100 GB 还是 1 TB;ChatGPT 每用户总量(摘要里是 25 GB,也见过 10 GB 的说法);Gemini PDF 1000 页还是 3000 页;ALB 超时上限 180 还是 600。
2. **Claude 对话 500 MB**:官方帮助页摘要确认是 500 MB / 每对话 20 个 / PDF 1000 页;不少第三方文章还写 30 MB(那是项目的上限)。
3. **没搜到官方数字的**:豆包(方舟)图片上限、智谱图片与文件上限(只有 2024 年的第三方文章)、Kimi 单图上限、Coze 知识库与对话附件上限、AnythingLLM 的上传大小上限、Dify 云各套餐的单文件上限(第三方说付费档 50 MB,未证实)。
4. **ChatGPT / Claude 应用在浏览器里是否先缩图再上传**:没查到。
5. **Open WebUI、AnythingLLM 的上传路径**(流式还是整份缓冲):本地没有仓库,没看代码。
6. **Dify 本地仓库是 2025-04 的旧版**,现在的版本可能已经改了上传路径和默认值。
7. **FastGPT「经代理层分片」**是从注释和类型名读出来的,没跟到网络层。**Onyx 的预签名上传是否用于对话附件**没查到。
8. **RAGFlow 的 `docker/.env`** 被本机的读取规则拦了,相关数字改用 `docs/faq.mdx:327-329`。
9. **hermes-agent、openclaw 没有「网页上传大文件」这条路径**,它们是本地 / 消息渠道的智能体,只在图片处理和读文件上限两处有参考价值;对问题 1 没有贡献。
10. **「没有人用 tus」「没有人做断点续传」**是基于 9 个本地仓库的关键词搜索,不是穷尽的结论。
