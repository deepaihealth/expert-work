"""B-209 —— 知识库扫描页识别。

设计:``docs/superpowers/specs/2026-10-21-b209-kb-scan-ocr-and-figures-design.md``。

* ``pages`` —— PDF 逐页判定(三信号)与扫描页渲染,跑在解析进程池里;
* ``cleaning`` —— 识别输出的清洗与校验(纯函数);
* ``clients`` —— 两种接口形状的薄适配(``layout_parsing`` / ``chat_vlm``)与重试;
* ``recognizer`` —— 一份文档的识别:缓存 → 主模型 → 校验 → 兜底 → 失败 / 待识别,记账。
"""
