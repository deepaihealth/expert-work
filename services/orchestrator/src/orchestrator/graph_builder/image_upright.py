"""B-322 PR ① —— 图进主模型之前先转正(Path A)。

设计 ``docs/superpowers/specs/2026-11-19-b322-pathA-vision-parity-design.md`` §4.1。主模型自己
能看图时,``agent_node`` 每一步组装提示词视图的时候:

A. 压缩之后、挂工作区快照与渲染页段之前(:meth:`ImageOrienter.prepare`):

   1. 找要判的图:这一轮的图(状态 ``turn_image_refs``)里还没有结论的;以及(只在一轮的第一步、
      图还在视野里时)上一轮留下 ``pending`` / ``unjudged`` 的、或判它的模型已被撤销的。一步最多
      :data:`MAX_JUDGED_PER_STEP` 张,新图优先。
   2. 用主模型自己的「关思考」路由按 B-301 的问法问(:mod:`orchestrator.tools.orientation_judge`),
      整步限时 30 秒;只采信核实过的模型给的回答,按回答归类(多出 ``pending`` / ``unjudged``)。
   3. 定这一步每张图显示的角度,调模型**之前**生成转正图、放进进程内缓存并钉住整步
      (:mod:`orchestrator.image_views`);视图里换成「同一个 ref + view」的块。
B. 平台段合成之后(:func:`~orchestrator.graph_builder.platform_context.with_image_notes`):需要
   说明的图在它所在那条消息末尾追加一段 ``<platform-context>``。

检查点里的消息、对象存储的字节永远是原图;结论、图编号、本步显示的角度三个通道随本步的返回值
落状态(``AgentState.conversation_images`` / ``image_orientations`` / ``shown_image_angles``)。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from expert_work.common.observability import ExpertWorkComponent, expert_work_span
from expert_work.protocol.model_catalog import (
    orientation_judge_allowed,
    orientation_model_label,
    orientation_revoked,
)
from expert_work.protocol.multimodal import WORKSPACE_REF_PREFIX
from orchestrator.graph_builder.platform_context import IMAGE_NOTES_MARK
from orchestrator.image_views import (
    LOW_COPY_FALLBACK_TTL_S,
    UPRIGHT_VIEWS,
    VIEW_KEY,
    ImageView,
    PreparedView,
    UprightViewCache,
    image_digest,
    in_host_crop_slot,
    in_wide_host_crop_slot,
    low_res_copy,
    main_model_image_view_cache_total,
    upright_copy,
)
from orchestrator.multimodal import IMAGE_REF_BLOCK_TYPE, ImageResolver
from orchestrator.tools.image_orientation import (
    DEFINITIVE_CONCLUSIONS,
    ORIENTATION_MIN_BUDGET_S,
    ORIENTATION_QUESTION,
    ORIENTATION_QUESTION_VERSION,
    ORIENTATION_TIMEOUT_S,
    Answer,
    classify_main_model_answers,
    main_model_image_orientation_retry_total,
    main_model_image_orientation_revoked_total,
    main_model_image_orientation_skipped_total,
    main_model_image_orientation_total,
    main_model_orientation_switch_on,
    parse_orientation_reply,
)
from orchestrator.tools.orientation_judge import judge_orientation
from orchestrator.tools.region_zoom import image_size
from orchestrator.tools.seam_relook import SEAM_RELOOK_MIN_RUN_LEFT_S
from orchestrator.tools.vision import (
    _FULL_FRAME,
    _SYSTEM_PROMPT,
    _crop_in_thread,
    _stringify,
    _tile_slots,
)

if TYPE_CHECKING:
    from expert_work.protocol import ModelSpec
    from expert_work.runtime.cancellation import CancellationToken
    from orchestrator.llm import LLMCaller
    from orchestrator.tools._guards import TokenBudget
    from orchestrator.usage_metering import UsageMeter

logger = logging.getLogger(__name__)

#: 一步最多问几张图(每个 run 最多 8 张上传图,``control_plane.settings``);新图优先。
MAX_JUDGED_PER_STEP: Final = 8
#: 短边不到这么多像素的图不问(厂商会拒;B-301 见过 1x1 被拒)。
MIN_SIDE_PIXELS: Final = 32
#: run 剩不到这么多秒就不问(与接缝重看同一条线)。
MIN_RUN_LEFT_S: Final = SEAM_RELOOK_MIN_RUN_LEFT_S
#: 进程内记录最多几条(整步从检查点重跑时不重问;每条几百字节)。
MEMO_SIZE: Final = 512
#: 一步钉住的全分辨率转正图最多占全分辨率区的几分之几(审查 M2):总给别的步骤留地方。
STEP_PIN_SHARE: Final = 0.5

ROTATED_NOTE: Final = (
    "Image {i} of {n} in this message was stored turned; it is shown rotated {degrees} degrees "
    "clockwise. The user's file is unchanged."
)
UNDETERMINED_NOTE: Final = (
    "The text in image {i} of {n} in this message does not look upright and its orientation "
    "could not be determined, so values read from it may be wrong."
)
PENDING_NOTE: Final = (
    "The text in image {i} of {n} in this message does not look upright; its orientation has "
    "not been determined yet, so values read from it may be wrong."
)
AS_STORED_NOTE: Final = (
    "Image {i} of {n} in this message is shown as stored in this step, not turned upright."
)
#: 定论那几句末尾固定加的话:系统提示词说「最新的平台段取代之前的」,这些说明挂在各自的历史消息上。
STAYS_VALID: Final = " This note about the image stays valid for the rest of the conversation."
PENDING_STAYS_VALID: Final = " This note stays valid until the platform replaces it."

_NOTE_TEXT: Final = {
    "rotated": ROTATED_NOTE + STAYS_VALID,
    "undetermined": UNDETERMINED_NOTE + STAYS_VALID,
    "pending": PENDING_NOTE + PENDING_STAYS_VALID,
    "as_stored": AS_STORED_NOTE,
}


@dataclass(frozen=True)
class OrientationJudge:
    """主模型的「关思考」问方向路由(备用树只留核实且没撤销的节点)+ 记账。"""

    caller: LLMCaller
    #: 落 ``token_usage`` 一行 + 扣池(与 ``ask_image`` 同一口径)。
    meter: UsageMeter
    #: 剪过枝、关了思考的那棵模型树(构建期测试核它每个节点都核实)。
    model: ModelSpec

    def answered_by(self, response: AIMessage) -> str:
        """实际应答的模型 ``provider/name``(按盖章解析;没盖章时是配置的主模型)。"""
        provider, name = self.meter._served(response)
        return orientation_model_label(provider, name)


@dataclass(frozen=True)
class StepRun:
    """这一步的 run 上下文。"""

    run_id: str
    #: 一轮的首段 run id(审批续跑段是新 run id,首段不变)。
    turn: str
    #: 这是这一轮的第一步(状态 ``step_count`` 为 0)。
    first_step: bool
    #: run 还剩多少秒;``None`` = 不限时。
    remaining_s: float | None
    cancel: CancellationToken
    tenant_id: UUID | None = None
    user_id: UUID | None = None
    token_budget: TokenBudget | None = None
    #: 这条对话(状态所在的线程);进程内记录按它记。空 = 用 ``run_id``。
    thread: str = ""

    @property
    def conversation(self) -> str:
        return self.thread or self.run_id

    def expires_at(self) -> float:
        """这个 run 在本进程里的东西(小副本、降级决定)最晚留到什么时候。"""
        left = self.remaining_s if self.remaining_s is not None else LOW_COPY_FALLBACK_TTL_S
        return time.monotonic() + max(0.0, left)


@dataclass
class UprightStep:
    """一步的工作区:钉住的缓存条目与要落状态的新值。``release`` 由 ``agent_node`` 的 finally 调。"""

    views: UprightViewCache
    pinned: list[tuple[str, int]] = field(default_factory=list)
    pinned_bytes: int = 0
    update: dict[str, Any] = field(default_factory=dict)
    #: 这一步取字节失败的 ref(适配器会把它们换成提示文字;说明里的「第几张」不数它们)。
    unreadable: set[str] = field(default_factory=set)

    def release(self) -> None:
        self.views.unpin_full(self.pinned)
        self.pinned = []
        self.pinned_bytes = 0

    def state_update(self) -> dict[str, Any]:
        return dict(self.update)


@dataclass(frozen=True)
class _Shown:
    image_id: str
    degrees: int = 0
    variant: str = "full"
    note: str | None = None


@dataclass(frozen=True)
class _ToJudge:
    image_id: str
    key: str
    ref: str
    #: 上一次的结论(``None`` = 新图);``pending`` 只重问第 2 步。
    previous: Mapping[str, Any] | None


@dataclass
class ImageOrienter:
    """``agent_node`` 用的转正器(只装在 ``supports_vision`` 的构建里)。``judge`` 为 ``None``:
    主模型没核实,不判新图,已有结论照样显示。"""

    image_resolver: ImageResolver
    configured_model: str
    judge: OrientationJudge | None = None
    views: UprightViewCache = UPRIGHT_VIEWS
    #: (对话, 结论键) → 结论;(对话, ref) → (图编号, 图记录)。尽力而为、按副本:某一步判完了却
    #: 失败 / 被取消、状态没落下时,同一对话之后的步骤从这里补回去(审查 M1;整步重跑不重问)。
    _memo: OrderedDict[tuple[str, str], dict[str, Any]] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _registered: OrderedDict[tuple[str, str], tuple[str, dict[str, Any]]] = field(
        default_factory=OrderedDict, init=False, repr=False
    )

    def new_step(self) -> UprightStep:
        return UprightStep(views=self.views)

    # ------------------------------------------------------------------ A 段

    async def prepare(
        self,
        step: UprightStep,
        *,
        state: Mapping[str, Any],
        messages: Sequence[BaseMessage],
        run: StepRun,
    ) -> list[BaseMessage]:
        """A 段:判新图、定角度、预先生成并钉住转正图、换块。紧急开关关着时提示词原样。"""
        turn_refs = list(dict.fromkeys(_string_list(state.get("turn_image_refs"))))
        if not main_model_orientation_switch_on():
            # 关着:提示词与没有这个功能时逐字节相同;上一次留下的显示角度在状态里清掉。
            if state.get("shown_image_angles"):
                step.update["shown_image_angles"] = {}
            if run.first_step and turn_refs:
                main_model_image_orientation_skipped_total.labels(reason="switch_off").inc(
                    len(turn_refs)
                )
            return list(messages)
        view_refs = _refs_in_view(messages)
        if not turn_refs and not view_refs:
            if state.get("shown_image_angles"):
                step.update["shown_image_angles"] = {}
            return list(messages)
        started = time.monotonic()
        images: dict[str, dict[str, Any]] = dict(state.get("conversation_images") or {})
        records: dict[str, dict[str, Any]] = dict(state.get("image_orientations") or {})
        new_images: dict[str, dict[str, Any]] = {}
        new_records: dict[str, dict[str, Any]] = {}
        by_ref = {ref: image_id for image_id, rec in images.items() for ref in rec.get("refs", [])}
        # 之前某一步登记过、那一步却没落状态的图:从进程内记录补回来(审查 M1)。
        for ref in dict.fromkeys([*turn_refs, *view_refs]):
            remembered = self._registered.get((run.conversation, ref))
            if ref not in by_ref and remembered is not None:
                image_id, entry = remembered
                merged = _with_ref(images.get(image_id), entry, ref)
                images[image_id] = new_images[image_id] = merged
                by_ref[ref] = image_id
        turn_ids: list[str] = []
        for ref in turn_refs:
            known = by_ref.get(ref)
            if known is None:
                registered = await self._register(ref, images, step=step, run=run)
                if registered is None:
                    continue
                known, fresh_entry = registered
                images[known] = new_images[known] = fresh_entry
                by_ref[ref] = known
                self._keep(self._registered, (run.conversation, ref), (known, fresh_entry))
            turn_ids.append(known)
        in_view = list(dict.fromkeys(by_ref[r] for r in view_refs if r in by_ref))
        # 进程内记录比状态新的(整步重跑、或那一步失败没落状态),补进本步返回值。
        for image_id in {*turn_ids, *in_view}:
            key = _record_key(images[image_id])
            remembered_record = self._memo.get((run.conversation, key))
            if remembered_record is not None and _newer(remembered_record, records.get(key)):
                records[key] = new_records[key] = remembered_record
        selected = self._select(turn_ids, in_view, images, records, run)
        with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "image_orientation") as span:
            judged = await self._judge_all(selected, run, started)
            for item, record in judged:
                records[item.key] = new_records[item.key] = record
                self._keep(self._memo, (run.conversation, item.key), record)
            shown = await self._shown(in_view, set(turn_ids), images, records, step, run)
            _span_attributes(span, new_images, judged, started)
        if new_images:
            step.update["conversation_images"] = new_images
        if new_records:
            step.update["image_orientations"] = new_records
        angles = {s.image_id: s.degrees for s in shown.values()}
        if angles != (state.get("shown_image_angles") or {}):
            step.update["shown_image_angles"] = angles
        return _rewrite(messages, shown, by_ref, step.unreadable)

    async def _register(
        self,
        ref: str,
        images: Mapping[str, Mapping[str, Any]],
        *,
        step: UprightStep,
        run: StepRun,
    ) -> tuple[str, dict[str, Any]] | None:
        """新见到的这一轮的图:取字节、算哈希、记尺寸。字节取不到 → ``None``(适配器照旧换成提示
        文字)。短边不到 :data:`MIN_SIDE_PIXELS` 的照样登记,但不问(:func:`_judgeable`)。
        计数只在一轮的第一步计(同一张图不按步数重复计)。"""
        data = await self._bytes(ref, step)
        if data is None:
            if run.first_step:
                main_model_image_orientation_skipped_total.labels(reason="unreadable").inc()
            return None
        digest = image_digest(data)
        image_id = _image_id(digest, images)
        existing = images.get(image_id)
        if existing is not None:  # 同一张图(同样的字节)又传了一次
            return image_id, _with_ref(existing, existing, ref)
        size = image_size(data)
        entry: dict[str, Any] = {
            "sha": digest,
            "refs": [ref],
            "width": size[0] if size else None,
            "height": size[1] if size else None,
            "first_turn": run.turn,
        }
        if not _judgeable(entry) and run.first_step:
            main_model_image_orientation_skipped_total.labels(reason="tiny").inc()
        return image_id, entry

    async def _bytes(self, ref: str, step: UprightStep | None = None) -> bytes | None:
        if ref.startswith(WORKSPACE_REF_PREFIX):
            return None
        try:
            return (await self.image_resolver.resolve(ref)).data
        except Exception:
            logger.info("image_upright.unreadable", exc_info=True)
            if step is not None:
                step.unreadable.add(ref)
            return None

    @staticmethod
    def _keep(store: OrderedDict[Any, Any], key: Any, value: Any) -> None:
        store[key] = value
        store.move_to_end(key)
        while len(store) > MEMO_SIZE:
            store.popitem(last=False)

    def _select(
        self,
        turn_ids: Sequence[str],
        in_view: Sequence[str],
        images: Mapping[str, Mapping[str, Any]],
        records: Mapping[str, Mapping[str, Any]],
        run: StepRun,
    ) -> list[_ToJudge]:
        """要问的图:新图(还没有结论)优先;再判的只在一轮的第一步、图在视野里、上一次不是这一轮。"""
        fresh: list[_ToJudge] = []
        for image_id in dict.fromkeys(turn_ids):
            key = _record_key(images[image_id])
            if key in records or not _judgeable(images[image_id]):
                continue
            fresh.append(_ToJudge(image_id, key, images[image_id]["refs"][0], None))
        again: list[_ToJudge] = []
        if run.first_step:
            for image_id in dict.fromkeys(in_view):
                key = _record_key(images[image_id])
                record = records.get(key)
                if (
                    record is not None
                    and _judgeable(images[image_id])
                    and _retry_due(record, run.turn)
                ):
                    again.append(_ToJudge(image_id, key, images[image_id]["refs"][0], record))
        if self.judge is None:
            # 每张新图只在它那一轮的第一步计一次(再判的不计:没有路由,它们每轮都会「到期」)。
            if run.first_step and fresh:
                main_model_image_orientation_skipped_total.labels(reason="model_not_verified").inc(
                    len(fresh)
                )
            return []
        if (fresh or again) and run.remaining_s is not None and run.remaining_s < MIN_RUN_LEFT_S:
            main_model_image_orientation_skipped_total.labels(reason="deadline").inc(
                len(fresh) + len(again)
            )
            # 新图记成 unjudged(下一轮第一步再判一次);再判的维持原样。
            return [replace(item, previous={"deadline": True}) for item in fresh]
        return [*fresh, *again][:MAX_JUDGED_PER_STEP]

    async def _judge_all(
        self, selected: Sequence[_ToJudge], run: StepRun, started: float
    ) -> list[tuple[_ToJudge, dict[str, Any]]]:
        if not selected:
            return []
        orient_until = started + ORIENTATION_TIMEOUT_S

        async def all_of_them() -> list[dict[str, Any]]:
            return list(
                await asyncio.gather(
                    *(self._judge_one(item, run, orient_until) for item in selected)
                )
            )

        records = await run.cancel.run_cancellable(all_of_them())
        return list(zip(selected, records, strict=True))

    async def _judge_one(self, item: _ToJudge, run: StepRun, orient_until: float) -> dict[str, Any]:
        previous = item.previous or {}
        # 新图:None;再判:pending 只重问第 2 步、unjudged 从头问(这一次之后就是定论 / 停在
        # unjudged);被撤销的定论从头重判一次、按新的第一次算。
        retry_from = (
            None if item.previous is None or "deadline" in previous else _effective(previous)
        )
        is_retry = retry_from in ("pending", "unjudged")
        attempts = int(previous.get("attempts", 1)) + 1 if is_retry else 1

        def not_asked(reason: str) -> dict[str, Any]:
            return _record(
                "unjudged",
                0,
                {},
                {},
                attempts=attempts,
                turn=run.turn,
                model=self.configured_model,
                reason=reason,
                calls=0,
            )

        if previous.get("deadline"):
            return not_asked("deadline")
        data = await self._bytes(item.ref)
        if data is None:
            return not_asked("no_copy")
        digest = image_digest(data)
        lent: list[tuple[str, int]] = []
        first: tuple[Answer, str | None] | None = None
        if retry_from == "pending":
            models = previous.get("models")
            first = ("no", models.get("0") if isinstance(models, Mapping) else None)
        calls = 0

        async def candidate(degrees: int) -> dict[str, Any]:
            crop = (await _crop_in_thread(data, [_FULL_FRAME], degrees))[0]
            key = (digest, degrees)
            self.views.lend(key, "image/jpeg", crop.data)
            lent.append(key)
            return _view_block(item.ref, ImageView(degrees, "candidate"))

        async def ask(block: dict[str, Any], budget_s: float) -> tuple[Answer, str | None]:
            nonlocal calls
            calls += 1
            return await self._ask_once(block, budget_s, run)

        try:
            orientation = await judge_orientation(
                candidate=candidate,
                ask=ask,
                budget=lambda extra_s: _budget(orient_until, extra_s),
                first=first,
                log_prefix="image_upright",
            )
        finally:
            for key in lent:
                self.views.give_back(key)
        classified = classify_main_model_answers(
            orientation.answers,
            orientation.models,
            trusted=_trusted,
            final=is_retry,
        )
        if classified.unverified_answer:
            main_model_image_orientation_skipped_total.labels(
                reason="answer_from_unverified_model"
            ).inc()
        if orientation.reason == "deadline":  # 30 秒用完、没来得及问完(审查 L4)
            main_model_image_orientation_skipped_total.labels(reason="deadline").inc()
        main_model_image_orientation_total.labels(
            conclusion=classified.conclusion, degrees=str(classified.degrees)
        ).inc()
        if retry_from is not None:
            main_model_image_orientation_retry_total.labels(
                **{"from": retry_from, "result": classified.conclusion}
            ).inc()
        return _record(
            classified.conclusion,
            classified.degrees,
            orientation.answers,
            orientation.models,
            attempts=attempts,
            turn=run.turn,
            model=self.configured_model,
            reason=orientation.reason,
            calls=calls,
        )

    async def _ask_once(
        self, block: dict[str, Any], budget_s: float, run: StepRun
    ) -> tuple[Answer, str | None]:
        """问一次「正不正」:最多等 ``budget_s``(含排队等切块名额);照常扣池 / 记账;任何
        ``Exception`` 都变成 ``error``,从不往外抛(取消与 ``BaseException`` 照常往上走)。"""
        judge = self.judge
        assert judge is not None  # noqa: S101 - 只在有路由时调
        messages: list[BaseMessage] = [
            SystemMessage(content=_SYSTEM_PROMPT),
            HumanMessage(content=[{"type": "text", "text": ORIENTATION_QUESTION}, block]),
        ]
        try:
            async with asyncio.timeout(budget_s), _tile_slots():
                with expert_work_span(ExpertWorkComponent.ORCHESTRATOR, "orientation_check"):
                    response = await judge.caller(messages=messages, tools=[])
            await judge.meter.charge(
                response,
                tenant_id=run.tenant_id,
                user_id=run.user_id,
                token_budget=run.token_budget,
            )
            model = judge.answered_by(response)
        except TimeoutError:
            return "timeout", None
        except Exception as exc:
            logger.info("image_upright.orientation_not_answered err=%s", type(exc).__name__)
            return "error", None
        word = parse_orientation_reply(_stringify(response.content))
        return (word, model) if word is not None else ("off_topic", model)

    # ------------------------------------------------------------ 显示

    async def _shown(
        self,
        in_view: Sequence[str],
        this_turn: set[str],
        images: Mapping[str, Mapping[str, Any]],
        records: Mapping[str, Mapping[str, Any]],
        step: UprightStep,
        run: StepRun,
    ) -> dict[str, _Shown]:
        """视野里每张登记过的图这一步怎么显示;横的图的转正图在这里生成并钉住。"""
        shown: dict[str, _Shown] = {}
        turned: list[tuple[str, int]] = []
        for image_id in dict.fromkeys(in_view):
            record = records.get(_record_key(images[image_id]))
            if record is not None and _is_revoked(record):
                main_model_image_orientation_revoked_total.inc()
                record = None
            conclusion = record.get("conclusion") if record is not None else None
            if conclusion == "rotated":
                turned.append((image_id, int(record["degrees"])))  # type: ignore[index]
            elif conclusion in ("undetermined", "pending"):
                shown[image_id] = _Shown(image_id, note=conclusion)
            else:
                shown[image_id] = _Shown(image_id)
        if turned:
            shown.update(await self._pin(turned, this_turn, images, step, run))
        return shown

    async def _pin(
        self,
        turned: Sequence[tuple[str, int]],
        this_turn: set[str],
        images: Mapping[str, Mapping[str, Any]],
        step: UprightStep,
        run: StepRun,
    ) -> dict[str, _Shown]:
        """横的图怎么发(审查 M2):

        * 这一轮的新图优先用全分辨率,一次全拿到(放不下最多等
          :data:`~orchestrator.image_views.PIN_WAIT_S` 秒);
        * 历史里的图逐张、不等:全分辨率区还有地方就用,没有就小副本,小副本区也满就按原样;
        * 一步钉住的全分辨率图最多占全分辨率区的 :data:`STEP_PIN_SHARE`,别的步骤总还有地方;
        * 降过级的图在这个 run 里一直用降过的那一份(字节稳定,不来回翻)。"""
        out: dict[str, _Shown] = {}
        new_full: list[tuple[str, int]] = []
        old_full: list[tuple[str, int]] = []
        low: list[tuple[str, int]] = []
        for image_id, degrees in turned:
            mode = self.views.run_mode(run.run_id, images[image_id]["sha"])
            if mode == "raw":
                out[image_id] = _Shown(image_id, note="as_stored")
            elif mode == "low":
                low.append((image_id, degrees))
            elif image_id in this_turn:
                new_full.append((image_id, degrees))
            else:
                old_full.append((image_id, degrees))
        share = int(self.views.full_cap_bytes * STEP_PIN_SHARE)
        group: list[tuple[str, int, PreparedView]] = []
        for image_id, degrees in new_full:
            made = await self._prepared_full(image_id, images[image_id], degrees, step, run, out)
            if made is not None:
                group.append((image_id, degrees, made))
        if group:
            views = [p for _, _, p in group]
            size = sum(len(p.data) for p in views)
            if step.pinned_bytes + size <= share and await self.views.pin_full(views):
                step.pinned.extend(p.key for p in views)
                step.pinned_bytes += size
                out.update({i: _Shown(i, d, "full", "rotated") for i, d, _ in group})
            else:
                low.extend((i, d) for i, d, _ in group)
        for image_id, degrees in old_full:
            made = await self._prepared_full(image_id, images[image_id], degrees, step, run, out)
            if made is None:
                continue
            size = len(made.data)
            if step.pinned_bytes + size <= share and self.views.try_pin_full([made]):
                step.pinned.append(made.key)
                step.pinned_bytes += size
                out[image_id] = _Shown(image_id, degrees, "full", "rotated")
            else:
                low.append((image_id, degrees))
        for image_id, degrees in low:
            out[image_id] = await self._low_copy(image_id, degrees, images[image_id], step, run)
        return out

    async def _prepared_full(
        self,
        image_id: str,
        image: Mapping[str, Any],
        degrees: int,
        step: UprightStep,
        run: StepRun,
        out: dict[str, _Shown],
    ) -> PreparedView | None:
        """全分辨率转正图:缓存里有就用;没有就生成(占两个解图名额)。原图字节取不到 → 不说什么、
        按原样(适配器换成提示文字);放弃转正(怎么退都超、解不开)→ 第 3 级。两种都返回 ``None``
        并在 ``out`` 里写好。"""
        key = (str(image["sha"]), degrees)
        cached = self.views.get(key[0], ImageView(degrees))
        if cached is not None:
            return PreparedView(key, *cached)
        data = await self._bytes(str(image["refs"][0]), step)
        if data is None:
            out[image_id] = _Shown(image_id)
            return None
        try:
            made = await in_wide_host_crop_slot(upright_copy, data, degrees)
        except ValueError:
            made = None
        if made is None:
            self._as_stored(run, key[0], "copy_too_large")
            out[image_id] = _Shown(image_id, note="as_stored")
            return None
        main_model_image_view_cache_total.labels(outcome="precomputed").inc()
        return PreparedView(key, *made)

    async def _low_copy(
        self,
        image_id: str,
        degrees: int,
        image: Mapping[str, Any],
        step: UprightStep,
        run: StepRun,
    ) -> _Shown:
        """第 2 级:约 150 万像素的小副本,归给本 run;小区也满 → 第 3 级按原样。"""
        sha = str(image["sha"])
        key = (sha, degrees)
        cached = self.views.get(sha, ImageView(degrees, "low"))
        if cached is None:
            data = await self._bytes(str(image["refs"][0]), step)
            if data is None:
                return _Shown(image_id)
            try:
                cached = await in_host_crop_slot(low_res_copy, data, degrees)
            except ValueError:
                self._as_stored(run, sha, "copy_too_large")
                return _Shown(image_id, note="as_stored")
        if not self.views.hold_low(run.run_id, [PreparedView(key, *cached)], run.expires_at()):
            self._as_stored(run, sha, "pin_over_cap")
            return _Shown(image_id, note="as_stored")
        if self.views.run_mode(run.run_id, sha) != "low":
            main_model_image_view_cache_total.labels(outcome="low_res_copy").inc()
        self.views.set_run_mode(run.run_id, sha, "low", run.expires_at())
        return _Shown(image_id, degrees, "low", "rotated")

    def _as_stored(self, run: StepRun, sha: str, reason: str) -> None:
        """第 3 级:这张图在这个 run 剩下的步骤里一直按原样(不来回翻);下一轮重新试。"""
        if self.views.run_mode(run.run_id, sha) != "raw":
            main_model_image_orientation_skipped_total.labels(reason=reason).inc()
        self.views.set_run_mode(run.run_id, sha, "raw", run.expires_at())


def _span_attributes(
    span: Any,
    new_images: Mapping[str, Any],
    judged: Sequence[tuple[_ToJudge, Mapping[str, Any]]],
    started: float,
) -> None:
    span.set_attribute("image_orientation.new_images", len(new_images))
    span.set_attribute("image_orientation.asked", len(judged))
    for name, conclusions in (
        ("rotated", ("rotated",)),
        ("undetermined", ("undetermined",)),
        ("not_answered", ("pending", "unjudged")),
    ):
        span.set_attribute(
            f"image_orientation.{name}", sum(r["conclusion"] in conclusions for _, r in judged)
        )
    span.set_attribute(
        "image_orientation.extra_calls", sum(int(r.get("calls", 0)) for _, r in judged)
    )
    span.set_attribute("image_orientation.duration_ms", _ms_since(started))


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def seedable_orientations(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """委派时种进子代的结论:本轮图(``turn_image_refs``)的**定论**,撤销的除外。pending /
    unjudged 不种:否则几个子代会同时各自重判同一张图,还可能与父代判得不一样。"""
    turn_refs = set(_string_list(state.get("turn_image_refs")))
    images = state.get("conversation_images") or {}
    records = state.get("image_orientations") or {}
    if not turn_refs or not isinstance(images, Mapping) or not isinstance(records, Mapping):
        return {}
    seeded: dict[str, dict[str, Any]] = {}
    for image in images.values():
        if not isinstance(image, Mapping) or not turn_refs & set(image.get("refs", [])):
            continue
        key = _record_key(image)
        record = records.get(key)
        if (
            isinstance(record, Mapping)
            and record.get("conclusion") in DEFINITIVE_CONCLUSIONS
            and not _is_revoked(record)
        ):
            seeded[key] = dict(record)
    return seeded


def _judgeable(image: Mapping[str, Any]) -> bool:
    """尺寸认得出、短边不小于 :data:`MIN_SIDE_PIXELS` 的图才问。"""
    width, height = image.get("width"), image.get("height")
    return (
        isinstance(width, int) and isinstance(height, int) and min(width, height) >= MIN_SIDE_PIXELS
    )


def _record_key(image: Mapping[str, Any]) -> str:
    return f"{str(image['sha'])[:16]}:{ORIENTATION_QUESTION_VERSION}"


def _record(
    conclusion: str,
    degrees: int,
    answers: Mapping[str, Answer],
    models: Mapping[str, str | None],
    *,
    attempts: int,
    turn: str,
    model: str,
    reason: str | None,
    calls: int,
) -> dict[str, Any]:
    return {
        "conclusion": conclusion,
        "degrees": degrees,
        "answers": dict(answers),
        "models": dict(models),
        "attempts": attempts,
        "last_turn": turn,
        # 判断时配置的主模型 / 实际应答的模型(``models``)只作记录,不进键。
        "configured_model": model,
        # 只采信核实模型的回答,所以这一项恒为真;写进记录,不靠事后去目录里查。
        "verified_when_judged": True,
        "reason": reason,
        "calls": calls,
        # 进程内记录与状态谁新(审查 M1:整步重跑、或那一步没落状态时用进程内那份)。
        "judged_at": time.time(),
    }


def _trusted(label: str | None) -> bool:
    if label is None or "/" not in label:
        return False
    provider, _, name = label.partition("/")
    return orientation_judge_allowed(provider, name)


def _is_revoked(record: Mapping[str, Any]) -> bool:
    """给出这条结论的某个模型在撤销清单里(显示时现算,不改写状态)。"""
    models = record.get("models")
    if not isinstance(models, Mapping):
        return False
    return any(isinstance(m, str) and orientation_revoked(m) for m in models.values())


def _effective(record: Mapping[str, Any]) -> str:
    """显示与再判时这条结论算什么:被撤销的按 unjudged。"""
    if _is_revoked(record):
        return "revoked"
    return str(record.get("conclusion"))


def _retry_due(record: Mapping[str, Any], turn: str) -> bool:
    """上一轮的 pending / unjudged 只再判一次;被撤销的定论重判一次(覆盖旧的)。"""
    if record.get("last_turn") == turn:
        return False
    effective = _effective(record)
    if effective == "revoked":
        return True
    if effective in DEFINITIVE_CONCLUSIONS:
        return False
    return int(record.get("attempts", 1)) < 2


def _budget(orient_until: float, extra_s: float) -> float | None:
    """此刻再问一次最多等多久(同 B-301 ``AskImageTool._orientation_budget``)。"""
    spare = orient_until - time.monotonic()
    if spare < ORIENTATION_MIN_BUDGET_S + extra_s:
        return None
    return min(ORIENTATION_TIMEOUT_S, spare)


def _view_block(ref: str, view: ImageView) -> dict[str, Any]:
    return {"type": IMAGE_REF_BLOCK_TYPE, "ref": ref, VIEW_KEY: view.as_dict()}


def _image_blocks(msg: BaseMessage) -> list[Mapping[str, Any]]:
    if not isinstance(msg, HumanMessage) or not isinstance(msg.content, list):
        return []
    return [
        block
        for block in msg.content
        if isinstance(block, Mapping)
        and block.get("type") == IMAGE_REF_BLOCK_TYPE
        and isinstance(block.get("ref"), str)
    ]


def _refs_in_view(messages: Sequence[BaseMessage]) -> list[str]:
    """提示词视图里出现的上传图 ref(按出现顺序、去重)。"""
    refs = [str(block["ref"]) for msg in messages for block in _image_blocks(msg)]
    return [ref for ref in dict.fromkeys(refs) if not ref.startswith(WORKSPACE_REF_PREFIX)]


def _image_id(digest: str, images: Mapping[str, Mapping[str, Any]]) -> str:
    """图编号:``img_`` + 图哈希前 8 位;撞上了别的图(前缀相同、哈希不同)就加长,直到不撞(审查 L1)。"""
    for length in (8, 16, 64):
        image_id = f"img_{digest[:length]}"
        existing = images.get(image_id)
        if existing is None or existing.get("sha") == digest:
            return image_id
    return f"img_{digest}"


def _with_ref(
    current: Mapping[str, Any] | None, entry: Mapping[str, Any], ref: str
) -> dict[str, Any]:
    """图记录并上一个 ref(同一张图的字节传了两次);已有记录的以它为准。"""
    base = dict(current if current is not None else entry)
    refs = list(base.get("refs", []))
    if ref not in refs:
        refs.append(ref)
    return {**base, "refs": refs}


def _newer(remembered: Mapping[str, Any], stored: Mapping[str, Any] | None) -> bool:
    if stored is None:
        return True
    return float(remembered.get("judged_at", 0.0)) > float(stored.get("judged_at", 0.0))


def _rewrite(
    messages: Sequence[BaseMessage],
    shown: Mapping[str, _Shown],
    by_ref: Mapping[str, str],
    unreadable: set[str] | frozenset[str] = frozenset(),
) -> list[BaseMessage]:
    """视图里换块、打说明的标;没变的消息原样(identity 不变)。原消息从不改。"""
    if not shown:
        return list(messages)
    out: list[BaseMessage] = []
    for msg in messages:
        blocks = _image_blocks(msg)
        if not blocks:
            out.append(msg)
            continue
        total = sum(str(block["ref"]) not in unreadable for block in blocks)
        out.append(_rewritten(msg, total, shown, by_ref, unreadable))
    return out


def _rewritten(
    msg: BaseMessage,
    total: int,
    shown: Mapping[str, _Shown],
    by_ref: Mapping[str, str],
    unreadable: set[str] | frozenset[str],
) -> BaseMessage:
    """一条带图的消息:横的图块加 ``view``;要说明的图按「这条消息里的第几张」记进标里(适配器把
    文字全放在前、图全放在后,所以只能按序号说)。这一步取不到字节的图适配器会换成提示文字,
    不算「图」,序号与总数都不数它(审查 L7)。"""
    content: list[Any] = []
    notes: list[str] = []
    index = 0
    changed = False
    for block in msg.content:
        if (
            isinstance(block, Mapping)
            and block.get("type") == IMAGE_REF_BLOCK_TYPE
            and isinstance(block.get("ref"), str)
            and str(block["ref"]) not in unreadable
        ):
            index += 1
            image_id = by_ref.get(str(block["ref"]))
            decision = shown.get(image_id) if image_id is not None else None
            if decision is not None and decision.degrees:
                view = ImageView(decision.degrees, decision.variant)  # type: ignore[arg-type]
                block = {**block, VIEW_KEY: view.as_dict()}
                changed = True
            if decision is not None and decision.note is not None:
                notes.append(
                    _NOTE_TEXT[decision.note].format(i=index, n=total, degrees=decision.degrees)
                )
        content.append(block)
    if not changed and not notes:
        return msg
    update: dict[str, Any] = {"content": content}
    if notes:
        update["additional_kwargs"] = {**msg.additional_kwargs, IMAGE_NOTES_MARK: notes}
    return msg.model_copy(update=update)


def _string_list(raw: object) -> list[str]:
    if not isinstance(raw, list | tuple):
        return []
    return [item for item in raw if isinstance(item, str) and item]


def _ms_since(started: float) -> int:
    return round((time.monotonic() - started) * 1000)


__all__ = [
    "ImageOrienter",
    "OrientationJudge",
    "StepRun",
    "UprightStep",
    "seedable_orientations",
]
