"""B-301 PR 4 —— 知识库扫描页的方向:一个四分类小模型(0 / 90 / 180 / 270 度)。

只用在一处:PDF 里判为「要识别」的页,在逐页判定时(解析进程池里)判一次方向,结果随这一页的
判定一路带着走,之后不再推理。**绝不用在**用户上传的图片、工作区图片文件上。

* **模型**:PP-LCNet_x1_0_doc_ori 的官方 ONNX 导出(Apache-2.0),原样放在
  ``orientation_model/``(许可证与来源见同目录的 ``LICENSE`` / ``NOTICE``)。文件哈希钉在
  :data:`MODEL_SHA256`,对不上就拒绝加载。
* **推理**:onnxruntime(镜像里本来就有),会话单线程、不预分配内存池,每个解析 worker 第一次
  用到时建一次;图片 worker 和主进程从不建。
* **怎么判**:把这一页渲成短边 :data:`SHORT_SIDE` 像素的小图,取中间 :data:`CROP` 见方,按
  ImageNet 均值 / 方差归一化。标签 L = 页面内容被顺时针转了 L 度;把握 ≥ :data:`THRESHOLD`
  才转(:attr:`Orientation.rotation`)。这个模型的把握最高约 0.94(训练时的标签平滑),转对的页
  在 0.92~0.93,合成集里把不是文字的页判成要转的最高 0.887 —— 能用的线只在 0.89~0.92 之间。
* **失败不静默**:模型加载不了或推理出错,这一页按不转处理(入库照常),调用方记告警计数。
* **开关**:平台级,默认开(:func:`configure_page_orientation`,
  ``Settings.knowledge_scan_page_orientation``);只影响之后新判定的页。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

import numpy as np
import pymupdf
from PIL import Image

logger = logging.getLogger(__name__)

MODEL_FILE: Final = Path(__file__).with_name("orientation_model") / "pp_lcnet_x1_0_doc_ori.onnx"
#: The unmodified official ONNX export (see ``orientation_model/NOTICE``).
MODEL_SHA256: Final = "af9a0a4f317ff0709ce752067807f819cb15d883f8ecad89f28df1c6ee2d9c92"
#: The model's labels, in output order: degrees the page content is turned clockwise.
LABELS: Final = (0, 90, 180, 270)
#: A page is turned only at this confidence or above (user decision 2026-11-12; a
#: constant, not a per-tenant / per-agent setting).
THRESHOLD: Final = 0.9
SHORT_SIDE: Final = 256
#: The small render's longest side, in pixels (3 x the short side: any page up to
#: 1 : 3 is drawn whole); past it only the centre square is drawn.
SMALL_RENDER_MAX_EDGE: Final = 3 * 256
CROP: Final = 224
_MEAN: Final = np.array([0.485, 0.456, 0.406], np.float32)
_STD: Final = np.array([0.229, 0.224, 0.225], np.float32)
_INPUT: Final = "x"

_enabled = True


def configure_page_orientation(*, enabled: bool) -> None:
    """The platform switch (``Settings.knowledge_scan_page_orientation``, on by default)."""
    global _enabled
    _enabled = enabled


def page_orientation_enabled() -> bool:
    """Whether new page decisions ask the classifier."""
    return _enabled


@dataclass(frozen=True)
class Orientation:
    """What the classifier said about one page: the content is turned ``angle``
    degrees clockwise, with ``confidence``; ``model``: the SHA-256 of the model file
    that said it. ``failed``: the classifier could not be asked (the page is not turned)."""

    angle: int = 0
    confidence: float = 0.0
    model: str = ""
    failed: bool = False

    @property
    def rotation(self) -> int:
        """Degrees the page's content is turned clockwise, to be undone when it is
        rendered: the classifier's angle when it is sure enough, else 0."""
        if self.failed or self.confidence < THRESHOLD:
            return 0
        return self.angle


#: A page the classifier could not be asked about.
FAILED: Final = Orientation(failed=True)


def reading_suffix(orientation: Orientation | None) -> str:
    """What a turned page adds to how it is read (the render parameters of its cache
    key and its reader digest): the angle and the model that chose it. Empty for a
    page that is not turned — its cache key and digest stay what they were."""
    if orientation is None or not orientation.rotation:
        return ""
    return f";rotate={orientation.rotation};orientation_model={orientation.model}"


def cache_tags(orientation: Orientation | None) -> dict[str, object]:
    """What a turned page's cached reading records besides its page, bytes and reader."""
    if orientation is None or not orientation.rotation:
        return {}
    return {"rotation": orientation.rotation, "rotation_confidence": orientation.confidence}


class PageClassifier(Protocol):
    def classify(self, image: np.ndarray) -> Orientation:
        """The orientation of a page drawn as ``image`` (height x width x RGB, uint8,
        short side :data:`SHORT_SIDE`)."""


class OrientationModelError(RuntimeError):
    """The model file is missing, is not the pinned one, or will not load."""


def preprocess(image: np.ndarray) -> np.ndarray:
    """The model's input (1 x 3 x 224 x 224 float32) for a page drawn as ``image``:
    short side to :data:`SHORT_SIDE` (bilinear, only when it is not already), the
    centre :data:`CROP` square, ImageNet mean / std, channels first."""
    height, width = image.shape[:2]
    if min(height, width) != SHORT_SIDE:
        scale = SHORT_SIDE / min(height, width)
        size = (round(width * scale), round(height * scale))
        image = np.asarray(Image.fromarray(image).resize(size, Image.Resampling.BILINEAR))
        height, width = image.shape[:2]
    left, top = max(0, (width - CROP) // 2), max(0, (height - CROP) // 2)
    crop = image[top : top + CROP, left : left + CROP].astype(np.float32)
    normalized = (crop * np.float32(1 / 255) - _MEAN) / _STD
    return np.ascontiguousarray(normalized.transpose(2, 0, 1)[None])


class OnnxPageClassifier:
    """The model file, checked against :data:`MODEL_SHA256` and loaded into a
    single-threaded onnxruntime session with no memory pre-allocation."""

    def __init__(self, path: Path = MODEL_FILE, sha256: str = MODEL_SHA256) -> None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            msg = f"orientation model not readable: {exc}"
            raise OrientationModelError(msg) from exc
        digest = hashlib.sha256(data).hexdigest()
        if digest != sha256:
            msg = f"orientation model {path.name} has sha256 {digest}, expected {sha256}"
            raise OrientationModelError(msg)
        import onnxruntime as ort  # type: ignore[import-untyped]

        options = ort.SessionOptions()
        # The deployment's thread env vars do not reach onnxruntime; the pod has one core.
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        options.enable_mem_pattern = False
        options.enable_cpu_mem_arena = False
        options.log_severity_level = 3  # the export's unused initializers warn on every load
        try:
            # Loaded from the bytes just checked, not the path: nothing in between.
            self._session: Any = ort.InferenceSession(
                data, sess_options=options, providers=["CPUExecutionProvider"]
            )
        except Exception as exc:
            msg = f"orientation model will not load: {exc}"
            raise OrientationModelError(msg) from exc
        self.model = digest

    def scores(self, image: np.ndarray) -> np.ndarray:
        """The model's probabilities for :data:`LABELS` (it ends in a softmax)."""
        output = self._session.run(None, {_INPUT: preprocess(image)})[0]
        return np.asarray(output, np.float32).reshape(-1)[: len(LABELS)]

    def classify(self, image: np.ndarray) -> Orientation:
        scores = self.scores(image)
        best = int(np.argmax(scores))
        return Orientation(angle=LABELS[best], confidence=float(scores[best]), model=self.model)


class UnavailableClassifier:
    """The classifier of a process whose model would not load: every page fails
    (:func:`~.pages.analyse_pdf` does not even ask it)."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def classify(self, image: np.ndarray) -> Orientation:
        raise OrientationModelError(str(self._error))


_classifier: OnnxPageClassifier | None = None


def process_classifier() -> PageClassifier:
    """This process's classifier, created on first use — only ever called in a parse
    worker. A model that will not load is logged and fails every page of this
    document; the next document tries again."""
    global _classifier
    if _classifier is None:
        try:
            _classifier = OnnxPageClassifier(MODEL_FILE, MODEL_SHA256)
        except Exception as exc:
            logger.warning("knowledge.page_orientation_model_unavailable", exc_info=True)
            return UnavailableClassifier(exc)
    return _classifier


def session_loaded() -> bool:
    """Whether this process holds an inference session."""
    return _classifier is not None


def small_render(page: pymupdf.Page) -> np.ndarray:
    """``page`` drawn with its short side :data:`SHORT_SIDE` pixels (RGB). A page
    whose long side would pass :data:`SMALL_RENDER_MAX_EDGE` pixels (a long thin
    strip) is drawn only around its centre square — all the model looks at
    (:func:`preprocess` takes the centre :data:`CROP`) — so no page allocates
    a large pixmap."""
    zoom = SHORT_SIDE / min(page.rect.width, page.rect.height)
    matrix = pymupdf.Matrix(zoom, zoom)
    whole = (page.rect * matrix).round()  # the pixels the whole page is drawn on
    clip = None
    if max(whole.width, whole.height) > SMALL_RENDER_MAX_EDGE:
        left = whole.x0 + (whole.width - SHORT_SIDE) // 2
        top = whole.y0 + (whole.height - SHORT_SIDE) // 2
        # A quarter pixel inside the square's edges: exactly those whole pixels, on
        # the grid of the whole page drawn (MuPDF scales embedded images a little
        # differently once clipped — under one level on average, measured 2026-11-12
        # — which is why pages of ordinary shape are drawn whole).
        square = pymupdf.Rect(
            left + 0.25, top + 0.25, left + SHORT_SIDE - 0.25, top + SHORT_SIDE - 0.25
        )
        clip = square * ~matrix
    pixmap = page.get_pixmap(matrix=matrix, clip=clip, alpha=False, colorspace=pymupdf.csRGB)
    return np.frombuffer(pixmap.samples, np.uint8).reshape(pixmap.height, pixmap.width, 3)


def classify_page(page: pymupdf.Page, classifier: PageClassifier) -> Orientation:
    """The page's orientation; :data:`FAILED` when it cannot be had (never raises)."""
    try:
        return classifier.classify(small_render(page))
    except Exception:
        logger.warning("knowledge.page_orientation_failed page=%s", page.number, exc_info=True)
        return FAILED
