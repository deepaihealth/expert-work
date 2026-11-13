#!/usr/bin/env bash
# CM-N5 一键基线跑 — LongMemEval + LoCoMo 全量（层 1 检索 + 层 2 端到端）。
#
# 用法（repo 根目录执行）：
#   tools/eval/run_baseline_full.sh smoke       # 连通性冒烟（fixture，几分钱，~1 分钟）
#   tools/eval/run_baseline_full.sh retrieval   # 层 1 检索基线（3 数据集 x 4 消融臂）
#   tools/eval/run_baseline_full.sh endtoend    # 层 2 端到端 QA（locomo + longmemeval_s，断点续跑）
#   tools/eval/run_baseline_full.sh full        # 全部（默认）：smoke -> retrieval -> endtoend
#
# 凭证：~/.expert-work-eval.env（EXPERT_WORK_EVAL_ENV 可覆盖路径），四行：
#   EXPERT_WORK_EVAL_EMBED_API_KEY / EXPERT_WORK_EVAL_EMBED_MODEL /
#   EXPERT_WORK_EVAL_LLM_API_KEY   / EXPERT_WORK_EVAL_LLM_MODEL
#
# 可调：EXPERT_WORK_EVAL_CONCURRENCY（默认 8，DashScope 限流报 429 就调小）。
# 反复跑安全：embedding 有 sqlite 缓存（不重复计费）；端到端按题断点续跑
# （eval-out/*.jsonl，已答题不重跑）；数字幂等合并进
# tools/eval/baselines/longmem_baseline.yaml。
set -euo pipefail

cd "$(dirname "$0")/../.."

ENV_FILE="${EXPERT_WORK_EVAL_ENV:-$HOME/.expert-work-eval.env}"
MODE="${1:-full}"
RUN="uv run python tools/eval/run_longmem.py"
# B-300:端到端档必须说明被测代码有没有显式的「用户文字」入口(run_longmem.py 二选一)。
# B-300 PR 1 合并之前是 --pre-b300;之后改成 --expect-user-text(也可用环境变量覆盖)。
USER_TEXT_FLAG="${USER_TEXT_FLAG:---pre-b300}"

if [[ ! -f "$ENV_FILE" ]]; then
    echo "ERROR: env file not found: $ENV_FILE" >&2
    exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
# Resolved AFTER sourcing so EXPERT_WORK_EVAL_CONCURRENCY can live in the env
# file. Precedence: env file > shell-exported value > default 8.
CONCURRENCY="${EXPERT_WORK_EVAL_CONCURRENCY:-8}"
for var in EXPERT_WORK_EVAL_EMBED_API_KEY EXPERT_WORK_EVAL_EMBED_MODEL EXPERT_WORK_EVAL_LLM_API_KEY EXPERT_WORK_EVAL_LLM_MODEL; do
    if [[ -z "${!var:-}" ]]; then
        echo "ERROR: $var missing in $ENV_FILE" >&2
        exit 1
    fi
done

log() { printf '\n[%s] === %s ===\n' "$(date '+%H:%M:%S')" "$*"; }

run_smoke() {
    log "smoke: fixture retrieval (fake embedder, free)"
    $RUN --benchmark fixture_longmemeval --tier retrieval --arms default,no_decay
    log "smoke: fixture endtoend (real Qwen, a few cents — validates both keys)"
    $RUN --benchmark fixture_locomo --tier endtoend --embedder real "$USER_TEXT_FLAG" \
        --results eval-out/smoke_locomo.jsonl --concurrency 2
    log "smoke OK — keys and endpoints are live"
}

run_retrieval() {
    for benchmark in locomo longmemeval_oracle longmemeval_s; do
        log "retrieval: $benchmark (4 arms, cached embeddings)"
        $RUN --benchmark "$benchmark" --tier retrieval \
            --arms default,vector,no_decay,no_mmr \
            --embedder real --concurrency "$CONCURRENCY" --update-baseline
    done
}

run_endtoend() {
    log "endtoend: locomo full (resumable — rerun this script to continue)"
    $RUN --benchmark locomo --tier endtoend "$USER_TEXT_FLAG" \
        --embedder real --concurrency "$CONCURRENCY" \
        --results eval-out/locomo_endtoend.jsonl --update-baseline
    # S re-scoped to a 150-question stratified sample (2026-06-10 user
    # decision): ingestion LLM cost dominates; locomo full covers the
    # cross-vendor comparison. Full-S stays available for milestone runs
    # (drop --sample).
    log "endtoend: longmemeval_s sample=150 (resumable)"
    $RUN --benchmark longmemeval_s --tier endtoend --sample 150 "$USER_TEXT_FLAG" \
        --embedder real --concurrency "$CONCURRENCY" \
        --results eval-out/longmemeval_s_endtoend.jsonl --update-baseline
}

case "$MODE" in
    smoke) run_smoke ;;
    retrieval) run_retrieval ;;
    endtoend) run_endtoend ;;
    full) run_smoke && run_retrieval && run_endtoend ;;
    *)
        echo "usage: $0 [smoke|retrieval|endtoend|full]" >&2
        exit 1
        ;;
esac
log "done — baselines: tools/eval/baselines/longmem_baseline.yaml"
