# NAS SetDirQuota 可行性调研(2026-08-10,波 3 § 七;只出结论不接线)

## 背景与现状

- **挂载形态**:静态 PV `workspace-nas`(`nasplugin.csi.alibabacloud.com`,
  `path=/workspaces`,NFSv3;`infra/k8s/base/control-plane/workspace-nas.yaml`)。
  control-plane 挂整树;沙箱 `pvName + subPath={tenant}/{user}`(`agent_sandbox.py`
  csi-volume-config)。**没有**走 CSI 动态供给的 `volumeAs: subpath +
  volumeCapacity: "true"` 那条路(那条才是「PVC storage 映射目录硬配额」,仅容量型,
  见 docs/research/2026-07-28-storage-selection.md)。
- **应用层现状**:PR-1 双闸 + 三层记账 + janitor 全量扫已闭环,自救路 = 删文件。

## 问 1:我们的 CSI 挂载方式下可行吗?

SetDirQuota 是 NAS OpenAPI(对文件系统内指定目录设配额,支持 user/group 维度),
配额落在服务端目录上,与客户端怎么挂载(静态 PV、subPath)解耦 → 机制上可行。

[待核实] 支持范围:通用型 NAS 支持、极速型不支持——我们的实例类型需控制台/
`DescribeFileSystems` 确认;若为极速型则直接不可行。

## 问 2:500 配额目录/文件系统上限的含义?

配额目标 = `{tenant}/{user}` 目录,数量 = 活跃用户数。500 上限 → 只够 ~500 用户,
多租户 SaaS 规模下**不能**做全量二道闸。降级为「每租户一条」只能限租户总量,与
本波「每用户上限」语义不匹配(spec § 十已明确不做租户级聚合)。

## 问 3:要不要作为二道硬闸叠加?

**结论:不叠加,不接线。**

1. 500 上限挡死全量覆盖(问 2);
2. 应用层闸已闭环且有人话自救路径;SetDirQuota 超限表现为沙箱内 NFS 写 EDQUOT/EIO,
   工具报错文案不可控,体验劣化;
3. OpenAPI 带外状态又一套,易与租户配额页漂移。

**保留的点杀用法(入 BACKLOG)**:单个惯犯用户在 janitor 30 分钟窗口内狂写、绕过
增量记账时,可对其目录手工 SetDirQuota(500 上限内个案无压力),作运维手段不作产品面。

## 引用

- docs/research/2026-07-28-storage-selection.md(volumeAs/volumeCapacity 仅容量型 + 500 上限出处)
- docs/superpowers/specs/2026-08-03-sandbox-migration-design.md(「500 目录上限是硬伤」原判)
- infra/k8s/base/control-plane/workspace-nas.yaml(挂载形态)
