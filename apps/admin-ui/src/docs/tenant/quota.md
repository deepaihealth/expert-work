---
title: 配额与用量
order: 6
---

## 租户配额

目前左侧菜单没有直接入口，需要在地址栏手动打开 `/settings/tenant-quotas`（页面标题「租户配额」）。这里管理本租户在各个维度上的用量上限，一共 8 个维度：

- `qps` — 每秒请求数
- `tokens_per_day` — 每日 token 用量上限
- `sandboxes` — 并发沙箱数
- `monthly_token_budget` — 每月 token 预算
- `image_upload_count_30d` — 30 天内图片上传次数
- `image_storage_bytes` — 图片存储总字节数
- `artifact_download_count_30d` — 30 天内产物下载次数
- `workspace_bytes_per_user` — 每个用户的工作区字节上限

其中 `image_storage_bytes` 和 `workspace_bytes_per_user` 是按字节计的维度，表格和「新建配额」表单里都会顺带把数值换算成人类可读的 GiB/MiB 提示，方便核对填的是不是你想要的量级。

点右上角「新建配额」，选一个维度、填限额（必填）和可选的 burst（允许的瞬时突发上限），保存即可；某个维度没有单独配置时，会沿用平台默认值，直到你显式创建一条为止。

如果你（平台管理员）当前停在「全部租户」的聚合视图，这个页面会提示需要先切到一个具体租户才能查看和编辑。

## 用量

左侧「设置」分组下点「用量」（`/settings/usage`）。默认显示当前租户本月的计费成本汇总和实时 token 计数，可以用「按智能体」/「按模型」切换分组视角，用日期选择器切换查看别的月份。计费成本数据每小时更新一次（页面上会标注「截至几点」），token 计数是实时的。表格里还能看到输入 / 输出 / 缓存写入 / 缓存读取 token 明细和缓存命中率。
