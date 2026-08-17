# 对外附件模型统一 —— 设计说明(2026-08-17)

**背景**:用户审阅对外文档时指出三处让对接方看不懂的地方,查代码后确认都是接口层的问题,
不是文档措辞问题:

1. 带图片有两条路 —— 顶层 `image_refs` 与 `files[]` 里 `type=image` 的项 —— 后端把两者合并,
   对外却像两个功能。
2. 文档与图片的 `upload_id` **字段同名、格式完全不同**(`uploads/report.pdf` vs
   `expert_work://image/{tenant}/{thread}/{id}.png`),因为落地存储不同:文档进终端用户的
   工作区卷,图片进对象存储并由既有 `image_upload` 表登记。第三方被迫理解底层实现。
3. 图片上传后**没有任何端点能把字节读回来**(全 control-plane 只有上传 / 删除 / 喂模型三条路),
   第三方前端无法回显;文档倒是能经 `GET …/workspace/file?path=uploads/…` 下到,但两种附件下载
   方式不同,又是一处不对称。

**前提**:对外 API 尚未上生产,**没有兼容包袱**——直接把形状改对,不留旧路。

**决策(用户 2026-08-17 拍板「按推荐做」)**:A+B+C 一个 PR 先做,文档随后按最终形状写。

---

## 一、对外契约(改后)

### 1. 上传 —— `POST /v1/agents/{agent_code}/uploads`(不变的部分省略)

响应 `data` 改为:

```json
{
  "upload_id": "upl_3f2c9a1e-7b44-4d3e-9c1a-2f6d0e8b5a17",
  "session_id": "…",
  "type": "document",
  "mime": "application/pdf",
  "size": 235112
}
```

- `upload_id` 统一为 **`upl_` + UUID**(小写、带连字符),文档与图片长得一样。
  第三方**原样回传、原样用于下载**,不需要也不应该解析它。
- `type` 仍返回(`"image"` / `"document"`),供 UI 显示;**不再要求第三方回传**。

### 2. 发起对话 —— `POST /v1/agents/{agent_code}/runs`

- **删除**顶层 `image_refs` 字段。请求体里出现该字段 → 422(`extra="forbid"` 已是既有行为)。
- `files[]` 每项只剩一个字段:

```json
{ "files": [ { "upload_id": "upl_…" }, { "upload_id": "upl_…" } ] }
```

  `type` / `transfer_method` **删除**——附件是图片还是文档,服务端凭 `upload_id` 查表得知,
  第三方不必也不能声明。上限仍为 64 项;其中图片数不得超过既有 `MAX_RUN_IMAGE_REFS`。
- 校验语义:

| 情况 | 响应 | 码 |
|---|---|---|
| `upload_id` 不是 `upl_<uuid>` 形状 | 422 | `INVALID_UPLOAD_ID` |
| 查无此行 / 不属于该 `user_id` / 已软删 | 404 | `UPLOAD_NOT_FOUND`(统一的 404,不透露存在性,与 `SESSION_NOT_FOUND` 同款) |
| 图片类附件与本次 run 的会话不是同一段 | 404 | `UPLOAD_NOT_FOUND`(沿用既有「图片不能跨会话」规则,ADR-0004) |
| Agent 不支持视觉却带了图片 | 422 | 沿用既有码 |
| 图片超过 `MAX_RUN_IMAGE_REFS` | 422 | 沿用既有码 |

  文档类附件只要求属于同一 `user_id`(工作区是按用户的),不要求同会话——与今天一致。

### 3. 新增下载 —— `GET /v1/agents/{agent_code}/uploads/{upload_id}?user_id=…`

- 权限 `session:read`(`write` key 含读)。
- 成功:**裸字节**(不套信封),`Content-Type` = 上传时记录的 MIME;`Content-Disposition`
  沿用 `_artifact_mime.infer_content_type` 的白名单规则(图片与安全文本 inline、
  可执行内容强制 attachment、未知一律 attachment),`filename` 取上传时登记的名(文档是净化后的
  原始文件名;图片是 `{image_id}{ext}`,与 §二.3 一致,不是原始文件名);
  `X-Content-Type-Options: nosniff`。
- 失败:套 `{success:false, error}` 信封:

| 情况 | 响应 | 码 |
|---|---|---|
| `upload_id` 形状不对 | 422 | `INVALID_UPLOAD_ID` |
| `user_id` 未知 / 行不存在 / 不属于该用户 / 已软删 / 底层字节已被回收 | 404 | `UPLOAD_NOT_FOUND` |
| 底层读不动(权限,仅文档;图片是对象存储读取失败,除「对象不存在」外的其它错误)| 500 | `UPLOAD_CONTENT_UNAVAILABLE` |
| 存储通路未配置(图片走对象存储 / 文档走工作区,任一没接好)| 503 | `UPLOAD_CONTENT_UNAVAILABLE` |

- `agent_code` 只是 URL 结构的一部分,**不参与权限判定与过滤**(与 artifacts / sessions 同款,
  附件是 (tenant, user) 维度)。
- **不计配额**——与既有 `…/workspace/file` 下载一致(它也不计);产物下载计
  `artifact_download` 是另一条线的裁决,这里不跟。
- 审计:照 `external_artifacts.download_artifact` 是否 emit 的现状镜像(实施时查,保持同款)。

### 4. 不做

- 不加 `DELETE …/uploads/{id}`(没人要;图片有既有 TTL 回收,文档在工作区里可经工作区接口删)。
- 不加列表端点。
- 不改控制台平面的 `/v1/sessions/{thread_id}/uploads`(仍返工作区路径 / 图片 URI,前端自用)。
- `files[]` 保持对象数组而非字符串数组——日后加 `remote_url` 等只扩字段不改形状。

---

## 二、内部实现

### 1. 新表 `user_upload`(迁移 `0146_user_upload`)

照 `0028_image_upload.py` 的模板(含 `ENABLE ROW LEVEL SECURITY` + `tenant_isolation` policy):

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | UUID PK | 即对外 `upl_<id>` 的 UUID 部分 |
| `tenant_id` | UUID not null | |
| `user_id` | UUID not null | `tenant_user.id`(终端用户) |
| `thread_id` | UUID not null | 上传时绑定的会话 |
| `kind` | text not null, CHECK in ('image','document') | |
| `ref` | text not null | 图片:`expert_work://image/…` URI;文档:工作区相对路径 `uploads/<name>` |
| `mime_type` | text not null | |
| `size_bytes` | bigint not null, CHECK ≥ 0 | |
| `filename` | text not null | 上传时的原始文件名,已过既有净化(供 Content-Disposition) |
| `created_at` | timestamptz not null default now() | |
| `deleted_at` | timestamptz null | 预留;本 PR 只有 purge 会碰它(直接硬删) |

索引:`(tenant_id, user_id)`、`(tenant_id, thread_id)`。

**为什么图片也登记一行而不复用 `image_upload`**:一张表、一次查询、一种 id,对外形状才真正统一;
`image_upload` 继续负责图片字节的生命周期(TTL 回收、硬删)。图片被回收后 `user_upload` 行仍在,
下载端点查 `image_upload.get` 发现不在 → 404 `UPLOAD_NOT_FOUND`。这是可接受的最终一致。

### 2. `UserUploadStore`(`expert_work.persistence.user_upload`)

照 `image_upload/` 的三件套:`base.py`(ABC + dataclass `UserUpload`)、SQL 实现、in-memory 实现。
方法:`insert(...)`、`get(*, upload_id, tenant_id) -> UserUpload | None`(**不过滤 user_id,
调用方比对**,与 image_upload.get 同款)、`delete_all_for_user(*, tenant_id, user_id) -> int`。

**两个后端谓词必须逐字节同义**(本仓反复踩过 SQL↔内存 store 语义分叉)。

### 3. 上传端点(`external_uploads.py`)

落地成功后 `insert` 一行,`upload_id` 返 `f"upl_{row.id}"`。图片路径:`ref = image_ref.to_uri()`;
文档路径:`ref = doc_result.path`。`filename` 取既有净化后的名(文档路径叶子名 / 图片用
`{image_id}{ext}`)。

### 4. run 请求(`agents.py` `ExternalRunRequest` / `ExternalFileRef`)

- 删 `image_refs` 字段与合并逻辑;`ExternalFileRef` 只剩 `upload_id: str`(pattern 校验
  `^upl_[0-9a-f]{8}-…$` 由一个共享的 `parse_upload_id(s) -> UUID | None` 做,上传端点渲染、
  run 解析、下载解析三处共用)。
- 解析:逐项 `store.get` → 比对 `user_id` → 按 `kind` 分流:image → 校验 `thread_id` == 本次
  会话 → 并入内部 `image_refs`;document → `ref` 并入 `document_names`(既有 `_safe_document_name_or_422`
  仍过一遍,防御纵深)。
- **未命中不铸用户、不写审计**(读语义)。

### 5. 下载端点(`external_uploads.py` 新增 GET)

照 `external_artifacts.download_artifact` 的结构:`lookup_external_user_id(mint=False)` →
`store.get` → 比对 user → 分流:
- image:`parse_image_ref(ref)` → `image_upload.get(image_id, tenant)` 活跃 → `object_store.get(object_key)`
- document:`workspace_store.read_file(tenant, user, ref)`;`WorkspacePermissionError` **必须排在**
  `SandboxSupervisorError` 之前(子类;沙箱 W2-BUG-1 教训)。

### 6. purge

`purge/user_purge.py` 的级联里加 `user_upload.delete_all_for_user` 一步(计数进 `PurgeReport`),
与其它 store 同款「单步失败不阻断」。

### 7. 路由登记(每次都漏)

- `tests/test_console_lockdown.py::_EXTERNAL_AGENT_ROUTES` **和**
  `tests/test_external_only_gate.py::_EXTERNAL_ROUTES` 两张手工表都要加新 GET。
- 新路由挂在既有 `external_uploads` router 上,自动带 `tags=["external"]` + `reject_nul_path_params`
  + `external_only()`。

### 8. 文档(本 PR 内,只碰这几处;整站可读性另一条线)

- `guide/chat.md` §2.3 请求表(删 `image_refs` 行、`files` 行改说明)、§2.6 整节重写(两步流程 +
  下载)、删「三个容易踩的地方」里关于两种格式的两条。
- `guide/errors.md` 增 `INVALID_UPLOAD_ID` / `UPLOAD_NOT_FOUND` / `UPLOAD_CONTENT_UNAVAILABLE`,
  删 `INVALID_FILE_REF` / `INVALID_IMAGE_REF`(若不再产生)。
- `guide/query.md` 若提到 `workspace/file` 可下 `uploads/…`,补一句「附件请走 5.x 下载端点」。

---

## 三、测试要点(每条都要 break→red→restore→green 自证)

- 上传:文档 / 图片响应 `upload_id` 都匹配 `^upl_<uuid>$`;表里各一行,`kind`/`ref` 正确。
- run:`image_refs` 字段 → 422;`files[{upload_id}]` 图片进 `image_refs`、文档进 `document_names`
  (monkeypatch spy);跨用户 / 不存在 / 形状错三种拒绝各一条,**且要先把用户 seed 出来**
  (本仓四次「测试没走到被测分支」全是因为请求在 `end_user_id is None` 就短路);
  图片跨会话 → 404;`MAX_RUN_IMAGE_REFS` 仍生效。
- 下载:文档 inline/attachment 分流各一;图片 inline;`WorkspacePermissionError` → 500 而非 404
  (顺序变异要红);图片行在、`image_upload` 已回收 → 404;跨用户 → 404;`user_id` 未知 → 404 且
  不铸 `tenant_user`。
- 两张手工路由表 + 三个 tag 自审 + `test_route_plane_partition` 全绿。
- purge:有 upload 行的用户 purge 后行数为 0。
- SQL 集成:`user_upload` 迁移升降级 + store 三方法真库跑通(`DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock`)。
