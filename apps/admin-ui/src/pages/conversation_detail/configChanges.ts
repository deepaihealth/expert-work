/** 一个会话里「配置被改过」的位置。
 *
 *  Agent 配置改动**立刻对新一轮生效**(包括正在进行的会话),这是拍板过的
 *  语义:线上发现问题改一下能立刻止血。代价是同一个会话前后可能跑在两套配置
 *  上 —— 事后复盘时,「客户第 5 轮开始收到的答复不对」既可能是模型的问题,
 *  也可能是有人在第 4 轮之后动了提示词,而**没有任何东西提示你去看**。
 *
 *  ``agent_version`` 回答不了这个问题:配置页是原地编辑,版本号编辑前后一样。
 *  唯一的依据是每一轮记下的 ``agent_spec_sha256``(见 agent_run 那一列)。
 */

/** 一轮 run 里这个判断需要的部分 —— 刻意只收这两个字段,免得把整个
 *  ``ConversationRun`` 拖进纯函数的签名里。 */
export interface RunConfigStamp {
  created_at: string;
  agent_spec_sha256?: string | null;
}

/**
 * 返回「配置在第几轮之后变过」的轮次号(1 基,升序)。
 *
 * **null 不参与比较。** ``agent_spec_sha256`` 为 null 有两种成因:这一列上线
 * 前的历史 run,以及 run 在 Agent 构建成功之前就结束了(配额拒绝 / Agent 被
 * 停用 / 构建失败)。两种都**不是**「用了另一套配置」—— 把 null 当成一个值去
 * 比,会在整个历史数据上凭空报出一堆并不存在的变更,那比不报更糟。
 * 所以只在相邻两轮**都有**哈希时才比较。
 */
export function configChangePoints(runs: readonly RunConfigStamp[]): number[] {
  const ordered = [...runs].sort((a, b) => a.created_at.localeCompare(b.created_at));
  const points: number[] = [];
  for (let i = 1; i < ordered.length; i += 1) {
    const before = ordered[i - 1].agent_spec_sha256;
    const after = ordered[i].agent_spec_sha256;
    if (!before || !after) continue;
    if (before !== after) points.push(i);
  }
  return points;
}
