# 从旧的手写排版迁移（migration）

面向之前自己写过排版脚本、手上还有旧风格记录的场景。

1. 旧的排版脚本（例如工作区里自己写的 `style/*.py` 之类）不再使用，也不要删除、不要再执行。渲染统一改走
   本技能的 `render.py`。
2. 把旧风格记录文件翻出来，逐条区分两类偏好：
   - **视觉类偏好**：主色、背景色、LOGO 位置、页脚对齐、某个板块用表格还是卡片、图表还是表格等。逐条翻译成
     `reference/style-options.md` 里的键，用
     `python $EXPERT_WORK_SKILLS_DIR/health-plan-report/scripts/resolve_style.py --merge-into report-style/personal.json --set 键=值`
     写进个人默认文件。
   - **内容类偏好**：板块构成与顺序、放哪些数据、要不要某一类信息。这些不属于样式，留在原记录里，继续由你
     自己在写内容 JSON（`reference/content-schema.md`）时按原有习惯遵守，本技能不会替你决定。
3. 有些视觉偏好当前换算不了（比如指定某种自定义字体、自定义背景图），如实告诉用户现在做不到，不要假装应用了。
4. 迁移只做一次；做完之后一律以 `report-style/personal.json` 为准，不用每次都重新翻译旧记录。
