# platform-skills

平台自带的 `docx` / `pptx` / `xlsx` / `pdf` 四个技能的源码。**从零自写（clean-room）**：
不得打开、复制、改写 Anthropic 原版技能（`anthropics/skills` 仓库，或平台数据库里这四个技能的
第 1 版）的正文或脚本，只依据公开文档与本仓库自己的设计编写。原因见
`docs/superpowers/specs/2026-09-25-platform-office-skills-design.md` §0（Anthropic 原版技能是
proprietary 明文禁止移植/衍生）。

## 目录结构

```
platform-skills/
  README.md          本文件
  build.py            打包脚本（确定性 ZIP）
  import_in_pod.py    发布脚本（Task 8）
  shared/             共享脚本库；只存放，不自动打进任何包
    preview.py
    convert.py
    ...
  docx/  pptx/  xlsx/  pdf/    各技能目录，结构相同：
    SKILL.md          技能正文（frontmatter + 中文说明）
    skill.yaml        声明本技能要用哪些共享脚本（可省略）
    scripts/          技能自带脚本
  tests/
    conftest.py       PLATFORM_SKILLS 路径常量 + build/unpack 夹具
    test_build.py     打包规则测试（第一层）
    ...               后续任务补齐第一、二层其余测试
  dist/               build.py 的输出目录（不进版本库）
```

## `skill.yaml` 规则

只有一个键 `shared`：从 `platform-skills/shared/` 复制进本技能包内 `scripts/` 的文件名列表。

```yaml
shared:
  - preview.py
  - convert.py
```

- 不声明 `shared`（或没有 `skill.yaml`）= 不共享任何文件（D6：按技能显式声明分发，不声明不给）。
- 声明的文件必须存在于 `shared/` 下，否则 `build.py` 报错退出。
- 技能自带的 `scripts/` 下不能有和共享文件同名的脚本（会静默覆盖，禁止），否则 `build.py` 报错退出。
- 除 `shared` 外出现任何其它键，`build.py` 报错退出。

## 本地打包

```bash
python platform-skills/build.py                    # 打全部技能，输出到 platform-skills/dist/
python platform-skills/build.py --only docx,pptx    # 只打指定技能
python platform-skills/build.py --out /tmp/out      # 自定义输出目录
```

打包结果对相同源码逐字节相同（固定文件顺序与 ZIP 时间戳），保证平台导入端的 `content_hash`
幂等判断有意义：内容没变的技能重复导入不会产生新版本。

## 三层测试

1. **第一层（单元测试，普通 pytest，秒级，每个 PR 跑）**

   ```bash
   uv run --no-sync pytest platform-skills/tests -q
   ```

   打包规则（本任务）、引用完整性、平台导入同款校验、环境事实一致、frontmatter/正文长度约束
   （后续任务补齐）。

2. **第二层（真实沙箱镜像里跑脚本）**

   需要本机能连 Docker，并指定用于跑测试的沙箱镜像。两个环境变量：

   ```bash
   export DOCKER_HOST=unix:///Users/mac/.docker/run/docker.sock
   export EXPERT_WORK_SKILLS_TEST_IMAGE=<沙箱镜像 tag>
   uv run --no-sync pytest platform-skills/tests/test_in_image.py -q
   ```

   未设置 `EXPERT_WORK_SKILLS_TEST_IMAGE` 时，第二层测试整体 skip（不影响第一层）。

3. **第三层（测试环境真实验收，人工执行）**

   导入测试环境后，用探针 Agent 走一遍新建 Word/PPT/Excel/PDF、模板填充、幻灯片复制、格式转换、
   PDF 加水印，逐项核对 `run_event` 里确实打开了技能、跑了技能脚本、没有 JS 尝试、产物能打开。
   详见 spec §6.3。

## 发布

`import_in_pod.py bundle` 在本机把「自身源码 + 本地打好的 `.skill` 包（base64）」拼成一个自包含
的 Python 程序打到 stdout；不连集群。它自己 import `control_plane`/`expert_work`（只为了把源码读
出来拼进输出——生成阶段不连数据库、不碰 Redis），所以本机跑 `bundle` 子命令要带上平台依赖的
venv（`uv run --no-sync python ...`），裸 `python3` 会 `ModuleNotFoundError`。把打印结果接到 pod
里的 `python3 -` 才会真正导入：跑的是平台自己 ZIP 上传接口那一条 `_ingest_platform_skill_payload`
管线（解析 → 名称校验 → 内容审核 → Mini-ADR U-21 严格威胁扫描 → 幂等创建/加版本 → 审计），导入
成功会自动发一次跨副本 `platform_skill` 失效广播（Redis pub/sub），所有副本的内建 Agent 缓存立刻
感知，不用重启。

先只读预演（`--dry-run`：只解析 + 名称校验 + 审核 + 扫描 + 算 `content_hash` 并与线上当前版本
比较，不写库、不发失效、也不上传技能资产对象存储——即使配了 OSS 也不会真的 PUT）：

```bash
uv run --no-sync python platform-skills/build.py
export KUBECONFIG=~/.kube/expert-work-test.yaml   # 生产换成 ~/.kube/expert-work-prod.yaml
kubectl config current-context                    # 执行 exec 前务必确认连的是哪个集群
POD=$(kubectl -n expert-work get pods -l app.kubernetes.io/name=control-plane \
  --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}')
uv run --no-sync python platform-skills/import_in_pod.py bundle --dry-run platform-skills/dist/*.skill \
  | kubectl -n expert-work exec -i "$POD" -- python3 -
```

（`--field-selector=status.phase=Running` 避免滚动发布期间挑中一个正在 Terminating 的 pod；
`kubectl config current-context` 只是确认，不改变行为——`$KUBECONFIG` 指哪个集群这条命令就连哪个,
生产库操作前这一步不能省。）

确认 `would_create_version` 符合预期后，去掉 `--dry-run` 正式导入：

```bash
uv run --no-sync python platform-skills/import_in_pod.py bundle platform-skills/dist/*.skill \
  | kubectl -n expert-work exec -i "$POD" -- python3 -
```

每个包输出一行 JSON：`file`（本地文件名）/ `name`（技能正文里解析出的真实技能名，可能与文件名不同）
/ `status`（201 新建、200 内容未变、`"dry-run"`）/ `created` / `version` 或 `would_create_version`
/ `content_hash` / `runtime`（advisory：这份技能里的脚本能否在当前沙箱运行，仅供参考不影响导入）。

只要有任意一个包 201，就会尝试发一次跨副本失效广播，并且**不论后面还有没有包导入失败**都会打印这行
汇总（这一行由发布步骤自己打，不等整批做完）：确认 Redis PUBLISH 真的送达了至少一个副本时是
`{"invalidation": "published", "receivers": N}`（`N` 是 Redis 返回的接收方数量）；没配失效总线（未设
`EXPERT_WORK_QUOTA_REDIS_URL`）或送达数为 0 时是 `{"invalidation": "skipped", "reason": "..."}`，且
只要没有其它包导入失败，脚本会额外以非零退出码结束（提醒操作者：技能已经导入成功，但没有任何副本
确认收到失效信号）。

**"skipped" 出现时不要重跑这条命令补救**——重跑时所有包都已存在且内容未变，会全部变成 200，
`created` 全 False，脚本根本不会再尝试发布，等于什么也没做。正确处理二选一：
① `kubectl -n expert-work rollout restart deploy/control-plane` 强制所有副本重启、重新加载技能；
② 接受各副本最长 1800 秒（30 分钟，见 `control_plane/runtime.py` 里 `AgentRuntime.cache_ttl_s`）后
自然过期，到时候会自己读到新版本，代价是这段时间内已导入的技能变化可能在部分副本上还看不到。

任何一个包导入失败（校验/审核/扫描不过）都会让脚本抛出未捕获异常、非零退出，且不影响它之前已经
成功导入的包（那些包触发的失效发布不受影响，按上面的规则照常尝试一次）。

回滚 = 拿旧 commit 的技能源码重新打包再导入，平台会把旧内容存成**新版本**（`skill_version` 只增
不改，`content_hash` 幂等判断只比较"最新版本"，回滚到的是比当前版本更早的内容，所以一定会新建
一个版本号，不会是 200 不变）。做法：

```sh
git worktree add /tmp/ps-rollback <旧 commit>
# 在主仓库目录里跑（用仓库自己的 venv）；build.py 按它自己的位置找源码、写 dist/，与当前目录无关
uv run --no-sync python /tmp/ps-rollback/platform-skills/build.py
uv run --no-sync python /tmp/ps-rollback/platform-skills/import_in_pod.py bundle /tmp/ps-rollback/platform-skills/dist/<技能名>.skill \
  | kubectl -n expert-work exec -i <control-plane-pod> -- python3 -
git worktree remove /tmp/ps-rollback
```

三个容易踩的坑：从主仓库跑 `platform-skills/build.py` 打的是**当前**源码不是旧版；在 worktree 目录里跑
`uv run` 会建一个没有依赖（pyyaml 等）的空 venv；只 `git archive` 某个技能目录会缺 `shared/` 与 `build.py`，
打包直接报错。也不要用 `git checkout <旧 commit> -- platform-skills/<技能名>/`，它会静默覆盖当前工作区里
未提交的改动并留下脏树。
