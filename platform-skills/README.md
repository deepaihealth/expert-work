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

导入命令由 Task 8 的 `platform-skills/import_in_pod.py` 提供（本任务尚未实现，占位）。
Task 8 完成后本节会补全为完整的 `kubectl exec` 发布命令。
