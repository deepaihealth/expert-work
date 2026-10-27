# ruff: noqa: E501, RUF001 —— 合成文档是测试数据:中文全角标点与长行本身就是被测输入
"""平台形态测试集(B-206 批 0 / §9.1)—— 供 ``test_knowledge_doc_forms.py`` 驱动。

六种文档形态(B-206 设计 §4 原则 3),每种 2~3 篇合成文档,内容全部虚构、与任何
租户无关(虚构公司的员工手册、虚构团队的会议纪要、虚构的价目表、虚构的客服对话):

* Ⓐ 有标题结构的长文档(手册 / 合同 / 制度);
* Ⓑ 没有标题的长文本(会议纪要、纯文本导出);
* Ⓒ 短文档(通知、FAQ 条目、一页纸);
* Ⓓ 表格(csv / xlsx 价目表、库存表);
* Ⓔ 对话体(客服 / 销售聊天,一行一句);
* Ⓕ 扫描件 / 图片(没有文字层);混合 PDF(文字页夹着扫描页,B-209 逐页识别)。

每篇都是**上传时的原始字节**(docx / xlsx / pdf / pptx 在这里现场生成),测试把它们
走真实的解析 → 切块 → 落段路径。``units`` 是用户放进文档的文字,一行(或一格)
一条:每一条都必须原样落进某个分段,这是「不丢字」的尺子。
"""

from __future__ import annotations

import csv
import hashlib
import io
import struct
import zipfile
from dataclasses import dataclass
from xml.sax.saxutils import escape

import pymupdf
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches


@dataclass(frozen=True)
class FormDoc:
    """One synthetic upload: its form (``"A"`` … ``"F"``), an ASCII ``slug`` for
    test ids, the file name, raw bytes, and the text units a user put in it
    (lines / paragraphs / table cells)."""

    form: str
    slug: str
    filename: str
    raw: bytes
    units: tuple[str, ...]

    @property
    def id(self) -> str:
        return f"{self.form}-{self.slug}"


def _text_units(text: str) -> tuple[str, ...]:
    """Non-blank lines of a text / Markdown source, an ATX heading as its text."""
    units = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            stripped = stripped.lstrip("#").strip()
        if stripped:
            units.append(stripped)
    return tuple(units)


def _text_doc(form: str, slug: str, filename: str, text: str) -> FormDoc:
    return FormDoc(form, slug, filename, text.encode("utf-8"), _text_units(text))


# ---------------------------------------------------------------------------
# Ⓐ 有标题结构的长文档
# ---------------------------------------------------------------------------

_HANDBOOK_MD = """\
# 员工手册

本手册说明公司的基本工作制度,适用于全体员工。如有疑问,请联系人力资源部。

## 第一章 总则

### 1.1 目的

为规范日常管理、明确员工的权利与义务,营造公平、透明、高效的工作环境,公司依据国家有关法律法规并结合自身实际情况制定本手册。本手册是员工日常工作的基本依据,各部门制定的细则不得与本手册相抵触。

### 1.2 适用范围

本手册适用于公司全体正式员工、试用期员工及实习生。

### 1.3 解释与修订

本手册由人力资源部负责解释。公司可根据经营需要对本手册进行修订,修订内容自公布之日起生效,并通过内部公告通知全体员工。

## 第二章 工作时间与考勤

### 2.1 工作时间

公司实行每周五天工作制,标准工作时间为周一至周五上午九点至下午六点,中午十二点至下午一点为午休时间。因岗位性质需要实行轮班或弹性工作制的,由所在部门报人力资源部备案后执行。弹性工作制下,员工每日须在上午十点至下午四点的核心时段在岗,每周累计工作时间不少于四十小时。

### 2.2 打卡规则

- 员工每日上班、下班各打卡一次,打卡以考勤系统记录为准。
- 外出办事、出差或参加外部培训的,应提前在考勤系统中提交外出申请,经直属主管审批后视同正常出勤。
- 因系统故障未能打卡的,应在当日内提交补卡申请并说明原因,每月补卡次数不超过三次。
- 代他人打卡或委托他人代为打卡的,双方均按严重违纪处理。

### 2.3 迟到与早退

上班时间开始后十五分钟内到岗的,记为迟到;超过十五分钟未到岗且未办理请假手续的,当日按旷工半天处理。下班时间之前离岗的,记为早退。每月迟到或早退累计三次以内的,不扣发工资;超过三次的,自第四次起每次扣发当月绩效奖金的百分之五。

### 2.4 加班

因工作需要延长工作时间的,须事先在系统中提交加班申请,经部门负责人批准后方可计为加班。工作日加班优先安排调休,调休应在加班之日起三个月内使用完毕;休息日加班无法安排调休的,按国家规定支付加班工资。法定节假日加班的,按国家规定支付加班工资,不安排调休。

## 第三章 假期

### 3.1 年假

员工在公司连续工作满一年的,可享受带薪年假。工作年限满一年不满十年的,年假为五天;满十年不满二十年的,年假为十天;满二十年的,年假为十五天。年假原则上应在当年度内休完,确因工作原因无法休完的,经部门负责人批准可顺延至次年第一季度,逾期未休的视为自动放弃。员工申请年假应至少提前三个工作日提交申请;连续休假五天以上的,应至少提前两周提交申请,并做好工作交接。

### 3.2 病假

员工因病需要休息的,应在当日上班前通知直属主管,并在返岗后三个工作日内补交医疗机构出具的诊断证明。连续病假三天以内的,工资照常发放;超过三天的部分,按本人基本工资的百分之八十发放。

### 3.3 事假

### 3.4 婚假与陪产假

员工依法登记结婚的,可享受婚假十天。员工的配偶生育的,可享受陪产假十五天。婚假与陪产假期间工资照常发放,假期应在相关事项发生之日起一年内一次性休完。

## 第四章 差旅与报销

### 4.1 差旅标准

| 项目 | 一线城市 | 其他城市 | 说明 |
| --- | --- | --- | --- |
| 住宿(每晚) | 600 元 | 400 元 | 凭发票据实报销,超出部分自理 |
| 伙食补助(每天) | 100 元 | 80 元 | 按出差天数计算,无需发票 |
| 市内交通(每天) | 80 元 | 60 元 | 可乘坐公共交通或出租车 |
| 城际交通 | 二等座或经济舱 | 二等座或经济舱 | 特殊情况需事先审批 |

### 4.2 报销流程

1. 员工在费用发生后三十天内,通过报销系统提交报销申请,并上传发票及相关凭证。
2. 直属主管在三个工作日内完成审批。
3. 财务部在收到完整单据后五个工作日内完成审核;单据不全的,退回补充。
4. 审核通过的款项在每周三统一支付到员工工资卡。

### 4.3 不予报销的情形

以下费用不予报销:个人消费或与工作无关的支出;未经审批的超标准支出;无合法有效票据的支出(伙食补助除外);超过报销时限且无正当理由的支出。

## 第五章 信息安全

员工应妥善保管公司配发的电脑、手机等设备,不得将设备借给他人使用。离开座位时应锁定屏幕,下班时应关闭电脑或使其进入锁定状态。公司设备上不得安装来源不明的软件,确需安装的,应向信息技术部提交申请,经审核通过后由信息技术部统一安装。员工的系统账号仅限本人使用,不得将密码告知他人,也不得使用他人账号登录。密码长度不少于十二位,应同时包含大写字母、小写字母、数字和符号,每九十天至少更换一次。发现账号异常登录、密码疑似泄露或设备丢失的,应立即报告信息技术部,由信息技术部协助冻结账号、远程锁定设备并排查影响范围。公司的客户资料、合同、财务数据、源代码和内部文件均属于保密信息,员工不得以任何形式向公司以外的人员披露。确因工作需要向合作方提供资料的,应事先签署保密协议,并经部门负责人批准。对外发送的文件应使用公司统一的文件共享工具,并设置访问期限和访问密码,不得通过个人邮箱或个人网盘传送公司文件。员工离职时,应在最后工作日前交还全部公司设备和资料,删除个人设备中保存的公司文件,并配合信息技术部注销各类系统账号。

信息技术部每季度组织一次信息安全培训,新员工应在入职后一个月内完成线上安全课程并通过考试。公司会不定期开展模拟钓鱼邮件演练,员工收到可疑邮件时,不要点击其中的链接或附件,应使用邮箱中的「报告可疑邮件」按钮上报。会议室和公共区域不得讨论保密事项,打印的保密文件应及时取走,不再需要的纸质资料应放入碎纸箱统一销毁。在公共场所使用电脑办公时,应注意遮挡屏幕,不得连接来源不明的无线网络,远程访问公司系统必须通过公司提供的加密通道。

对违反本章规定的,公司将视情节轻重给予警告、通报批评直至解除劳动合同的处理;造成公司损失的,依法追究赔偿责任。

## 附录

### 附录 A 常用表单

### 附录 B 修订记录
"""

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOCX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Default Extension="png" ContentType="image/png"/>'
    '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    "</Types>"
)
_DOCX_PACKAGE_RELS = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_REL_NS}">'
    f'<Relationship Id="rId1" Type="{_R_NS}/officeDocument" Target="word/document.xml"/>'
    "</Relationships>"
)
_DOCX_STYLES = (
    f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:styles xmlns:w="{_W_NS}">'
    + "".join(
        f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/></w:style>'
        for n in (1, 2, 3)
    )
    + "</w:styles>"
)


def _docx(body_xml: str, *, image: bytes | None = None) -> bytes:
    """A minimal Word package — the parts MarkItDown (mammoth) reads: the
    document, its heading styles, and optionally one embedded PNG (``rId2``)."""
    image_rel = (
        f'<Relationship Id="rId2" Type="{_R_NS}/image" Target="media/image1.png"/>' if image else ""
    )
    document_rels = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="{_REL_NS}">'
        f'<Relationship Id="rId1" Type="{_R_NS}/styles" Target="styles.xml"/>{image_rel}'
        "</Relationships>"
    )
    document = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{_W_NS}"><w:body>{body_xml}</w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as package:
        package.writestr("[Content_Types].xml", _DOCX_CONTENT_TYPES)
        package.writestr("_rels/.rels", _DOCX_PACKAGE_RELS)
        package.writestr("word/_rels/document.xml.rels", document_rels)
        package.writestr("word/document.xml", document)
        package.writestr("word/styles.xml", _DOCX_STYLES)
        if image:
            package.writestr("word/media/image1.png", image)
    return buffer.getvalue()


def _docx_paragraph(text: str, style: str | None) -> str:
    props = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f'<w:p>{props}<w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>'


#: (style, text) — ``Heading1`` / ``Heading2`` become ``#`` / ``##`` after parsing.
_CONTRACT_PARAGRAPHS: tuple[tuple[str | None, str], ...] = (
    ("Heading1", "软件服务合同"),
    (None, "甲方(客户):示例科技有限公司"),
    (None, "乙方(服务方):样例软件有限公司"),
    (
        None,
        "甲乙双方本着平等自愿、诚实信用的原则,就乙方向甲方提供在线协同办公软件服务的有关事宜,经友好协商,订立本合同,以资共同遵守。",
    ),
    ("Heading2", "第一条 定义"),
    (
        None,
        "1.1 「服务」是指乙方通过互联网向甲方提供的在线协同办公软件,包括文档协作、任务管理、即时通讯及相关的技术支持服务。",
    ),
    (None, "1.2 「用户账号」是指甲方在服务中开通的、供其员工登录使用的账号。"),
    (None, "1.3 「甲方数据」是指甲方及其用户在使用服务过程中上传、存储或生成的全部数据。"),
    ("Heading2", "第二条 服务内容与期限"),
    (
        None,
        "2.1 乙方为甲方开通专业版服务,用户账号数量为两百个,存储空间为每账号五十 GB。甲方在服务期内需要增加账号的,按本合同附件的单价另行计费。",
    ),
    (
        None,
        "2.2 服务期限为一年,自乙方开通服务之日起计算。期满前三十日内双方均未书面提出终止的,本合同自动续期一年,续期次数不限。",
    ),
    ("Heading2", "第三条 费用与支付"),
    (
        None,
        "3.1 服务费用总额为人民币捌万元整(¥80,000.00),甲方应在本合同签订之日起十五个工作日内一次性支付。",
    ),
    (None, "3.2 乙方在收到款项后五个工作日内向甲方开具等额的增值税专用发票。"),
    (None, "3.3 甲方逾期付款的,每逾期一日,应按未付金额的万分之五向乙方支付违约金。"),
    ("Heading2", "第四条 服务水平"),
    (
        None,
        "4.1 乙方保证服务在每个自然月内的可用率不低于百分之九十九点九。可用率按当月服务实际可用的分钟数除以当月总分钟数计算,计划内维护时间不计入不可用时间,但乙方应提前三个工作日通知甲方,且每月计划内维护累计不超过四小时。",
    ),
    (
        None,
        "4.2 服务可用率低于约定标准的,乙方应按下列标准向甲方提供服务时长补偿:可用率低于百分之九十九点九但不低于百分之九十九的,补偿当月服务时长的百分之十;低于百分之九十九但不低于百分之九十五的,补偿当月服务时长的百分之二十五;低于百分之九十五的,补偿当月服务时长的百分之五十。补偿以延长服务期限的方式提供,不折算现金。",
    ),
    (
        None,
        "4.3 乙方提供工作日上午九点至下午六点的在线技术支持。一般问题在四个工作小时内响应;影响全部用户使用的严重故障,乙方应在三十分钟内响应,并在四小时内恢复服务或提供临时解决方案。",
    ),
    ("Heading2", "第五条 数据与保密"),
    (
        None,
        "5.1 甲方数据的所有权归甲方。乙方仅为向甲方提供服务的目的处理甲方数据,不得将甲方数据用于任何其他目的,也不得向任何第三方披露,但法律法规另有规定的除外。",
    ),
    (
        None,
        "5.2 乙方应采取不低于行业通行标准的技术和管理措施保护甲方数据的安全,包括但不限于传输加密、存储加密、访问控制和操作日志审计。发生或可能发生数据泄露的,乙方应在知悉后二十四小时内通知甲方,并采取补救措施。",
    ),
    (
        None,
        "5.3 本合同终止后三十日内,乙方应按甲方要求将甲方数据以通用格式交还甲方,并在交还完成后删除其保存的甲方数据副本。",
    ),
    ("Heading2", "第六条 违约责任"),
    (
        None,
        "任何一方违反本合同约定的,应赔偿对方因此遭受的直接损失。乙方在本合同项下的累计赔偿责任,以甲方已支付的服务费用总额为限,但因乙方故意或重大过失导致甲方数据泄露的除外。",
    ),
    ("Heading2", "第七条 争议解决"),
    (
        None,
        "因本合同引起的或与本合同有关的任何争议,双方应友好协商解决;协商不成的,任何一方均可向合同签订地有管辖权的人民法院提起诉讼。",
    ),
    ("Heading2", "第八条 其他"),
    (None, "本合同一式两份,甲乙双方各执一份,自双方盖章之日起生效。"),
)

_ENGLISH_POLICY_MD = """\
# Travel and Expense Policy

This policy explains how employees book business travel and claim back work-related expenses. It applies to all employees and contractors who travel on company business.

## 1. Purpose

The company wants travel to be safe, comfortable and cost-effective. Employees should spend company money as carefully as they would spend their own, and every claim must have a clear business reason.

## 2. Booking Travel

### 2.1 Air Travel

Book economy class for all flights shorter than six hours. Premium economy may be booked for flights of six hours or longer with the approval of a department head. Tickets should be booked at least fourteen days before departure whenever possible; last-minute bookings need a short written justification.

### 2.2 Hotels

Choose a standard room in a mid-range hotel close to the place of business. The nightly limit is 180 dollars in major cities and 120 dollars elsewhere, including taxes. If a conference hotel is more expensive, attach the conference booking page to the claim.

### 2.3 Ground Transport

Use public transport where it is practical. Taxis and ride-hailing services are allowed for trips to and from airports, late at night, or when carrying heavy equipment. Rental cars need approval in advance and should be the smallest category that fits the team and its luggage.

## 3. Meals

The daily meal allowance is 60 dollars in major cities and 45 dollars elsewhere. Alcohol is not reimbursed unless it is part of an approved client dinner. Keep itemised receipts for any single meal over 25 dollars.

## 4. Submitting a Claim

Submit every claim through the expense system within thirty days of the expense. Each line of the claim needs a receipt, the date, the amount in the original currency, and a one-line business reason that a reviewer who was not on the trip can understand. Receipts must be legible photos or the original electronic invoice; a bank or card statement on its own is not a receipt, because it does not show what was bought. When an expense was paid in a foreign currency, enter the amount in that currency and let the expense system apply the exchange rate of the transaction date; if your card provider charged a different rate, attach the card statement as well so the difference can be reimbursed. Group expenses, such as a team dinner, should be claimed by the most senior person present, who must list the names of all attendees and the reason for the meeting. Mileage for a private car is reimbursed at 0.55 dollars per mile, based on the shortest practical route; record the start and end address of each trip. Claims are reviewed first by the employee's manager, who checks the business reason, and then by the finance team, who check receipts, limits and tax rules. Approved claims are paid with the next payroll run. A claim that is returned for missing information keeps its original submission date as long as it is corrected within ten days. Claims submitted more than ninety days after the expense will be rejected unless the finance director approves a late submission. Lost receipts can be replaced by a signed missing-receipt form, which may be used at most twice a year and only for expenses under 75 dollars. Expenses that are personal, such as minibar items, in-room movies, upgrades chosen for comfort, or travel for family members, are never reimbursed, even when they appear on the same hotel invoice as approved costs; deduct them before you submit the claim.

## 5. Exceptions

## 6. Questions

Questions about this policy go to the finance team through the internal help desk. Questions about a specific claim should include the claim number shown in the expense system.
"""


# ---------------------------------------------------------------------------
# Ⓑ 没有标题的长文本
# ---------------------------------------------------------------------------

_MEETING_NOTES_TXT = """\
项目周会纪要

时间:三月十二日(周三)下午两点至三点半
地点:三楼会议室及线上会议
参会人:产品经理、后端负责人、前端负责人、测试负责人、运维工程师、设计师
记录人:项目助理

一、上周进展回顾。产品经理首先回顾了上周的整体进展:新版订单列表页已经完成开发和自测,进入测试阶段;导出功能的后端接口已完成,前端还差分页和筛选条件的联调;消息通知模块的设计稿已经定稿,开发计划下周启动。上周计划内的八项任务完成了六项,未完成的两项分别是批量导入和权限配置页,原因是接口文档变更了两次,前后端对字段含义的理解不一致,返工花了两天时间。

二、测试情况。测试负责人介绍,订单列表页本周共发现缺陷二十三个,其中严重缺陷两个:一是筛选条件为空时列表会返回全部历史数据,导致页面加载超过十秒;二是在某些浏览器下翻页后勾选状态丢失。其余二十一个为一般缺陷和界面问题,目前已修复十四个。测试负责人提醒,导出功能涉及大数据量,需要单独安排一轮性能测试,建议准备至少十万条订单的测试数据,并分别验证同步导出和异步导出两种方式。后端负责人补充说,同步导出目前设置的上限是五千条,超过上限会自动转为异步任务,完成后通过站内消息通知用户下载;异步任务的文件保留七天,过期自动清理。前端负责人提出,异步导出时页面上需要明确提示「导出任务已提交,完成后会通知您」,避免用户重复点击;同时导出记录页面要能看到每个任务的状态、提交时间和下载链接。设计师表示可以在本周五之前补充导出记录页的设计稿,沿用现有列表页的样式,不新增组件。运维工程师提醒,异步导出会在高峰期占用较多数据库资源,建议导出任务读取只读副本,并限制同一用户同时进行的导出任务不超过两个;如果后续导出量明显增加,再考虑单独部署导出服务。大家讨论后同意先按上述方案实现,上线后观察两周的资源占用情况再做调整。

三、上线计划。运维工程师说明,下周二晚上十点有一个常规发布窗口,建议订单列表页和导出功能一起发布。发布前需要完成以下准备:数据库新增两个索引,预计执行时间约十分钟,需要在发布窗口开始前执行;配置中心新增导出上限和文件保留天数两个参数;监控面板增加导出任务的成功率和平均耗时两个指标。如果发布后出现严重问题,回滚方案是切回上一个版本,新增的索引保留不删,不影响旧版本运行。

四、风险与问题。后端负责人提出,批量导入功能依赖的文件解析组件存在内存占用过高的问题,处理五万行以上的表格时可能导致服务重启,需要在下周评估替换方案。产品经理表示批量导入可以推迟到下一个迭代,本迭代优先保证订单列表和导出功能的质量。权限配置页的需求还有两处没有确认,产品经理会在周四前和业务方确认后更新需求文档。

五、下周计划。前端完成导出功能联调并修复剩余缺陷;后端完成导出性能优化和批量导入组件评估;测试完成订单列表回归测试和导出性能测试;设计师补充导出记录页设计稿;运维工程师准备发布方案并在周一前提交评审。下次周会时间为三月十九日下午两点,地点不变。
"""

_WORKLOG_EXPORT_TXT = "\n".join(
    [
        "工作日志导出 2026 年第三季度 运维值班组",
        "07-01 09:10 早班交接 夜间无告警 备份任务全部成功",
        "07-01 11:42 收到工单 部分用户反馈登录页加载慢 排查后确认为图片资源未走缓存 已调整缓存规则",
        "07-01 15:20 例行巡检 磁盘使用率最高的节点为 71% 低于告警阈值",
        "07-02 08:55 早班交接 夜间告警两次 均为短时网络抖动 已自动恢复",
        "07-02 10:30 配合开发组发布订单服务新版本 发布用时 18 分钟 无异常",
        "07-02 16:05 证书到期提醒 测试环境证书将在 14 天后到期 已提交续期申请",
        "07-03 09:00 早班交接 夜间无告警",
        "07-03 13:47 数据库慢查询告警 来源为报表导出任务 已与报表组沟通改为夜间执行",
        "07-03 17:30 更新值班手册 补充报表任务的排查步骤",
        "07-04 09:05 早班交接 夜间备份任务耗时比平时多 40 分钟 原因待查",
        "07-04 11:15 定位备份变慢原因 新增的日志表未设置清理策略 体积增长较快 已补充按月归档",
        "07-04 15:40 演练 切换备用负载均衡 切换用时 2 分 13 秒 业务无感知",
        "07-05 09:00 早班交接 夜间无告警 周末值班安排已确认",
        "07-05 14:22 收到工单 内部知识库搜索偶发超时 扩容一个搜索节点后恢复正常",
        "07-06 10:10 周末值班 巡检正常",
        "07-07 10:05 周末值班 巡检正常 机房空调例行维护 温度监控无异常",
        "07-08 09:00 早班交接 夜间告警一次 为消息队列积压 高峰过后自动消化",
        "07-08 11:30 评估消息队列扩容方案 建议将分区数从 6 调整为 12 待周会评审",
        "07-08 16:45 清理过期的临时账号 共 23 个 已通知各账号申请人",
        "07-09 09:02 早班交接 夜间无告警",
        "07-09 10:40 配合安全组完成季度漏洞扫描 高危 0 项 中危 3 项 已建工单跟进",
        "07-09 15:15 中危漏洞一项已修复 剩余两项涉及第三方组件 等待供应商补丁",
        "07-10 09:00 早班交接 夜间无告警",
        "07-10 14:00 周会 评审通过消息队列扩容方案 计划下周二凌晨执行",
        "07-10 17:20 编写扩容操作步骤和回滚步骤 提交评审",
        "07-11 09:00 早班交接 夜间告警一次 某台应用服务器内存使用率超过 90% 重启后恢复 已保留内存快照",
        "07-11 13:35 分析内存快照 发现缓存对象未设置过期时间 已反馈给开发组",
        "07-12 10:00 周末值班 巡检正常",
        "07-13 10:00 周末值班 巡检正常",
        "07-14 09:00 早班交接 夜间无告警 扩容准备工作完成",
        "07-15 02:00 执行消息队列扩容 用时 35 分钟 扩容后积压告警消失",
        "07-15 09:00 早班交接 扩容后运行平稳 继续观察一周",
    ]
)

_ENGLISH_NOTES_MD = """\
Weekly sync — platform team, notes taken by the team lead.

Attendees: the team lead, two backend engineers, one frontend engineer, the designer, and a support representative who joined for the first half.

The support representative opened with the top three customer complaints from the past week. First, password reset emails sometimes arrive after the link has already expired, because the queue that sends them backs up during the morning peak. Second, the mobile app logs people out after an update. Third, the export button is hard to find on small screens. Support asked whether any of these could be fixed before the end of the month, since the same questions now make up almost a third of all tickets.

The backend engineers explained that the email delay comes from a single worker that handles every outgoing notification. Under normal load it keeps up, but at nine in the morning it receives reminders, digests and password resets at the same time, and password resets wait behind thousands of digests. The proposal is to give password resets and other security emails their own queue with a dedicated worker, and to extend the reset link lifetime from fifteen to thirty minutes as a safety margin. They estimated three days of work including tests, and they want to add an alert that fires when any security email waits longer than one minute. The team lead asked whether a longer link lifetime weakens security; the engineers answered that thirty minutes is still within common practice as long as the link can be used only once and is invalidated as soon as the password changes, which is already the case. The team agreed to ship the separate queue first and to change the link lifetime in the same release.

- Mobile logout: the frontend engineer traced it to the app clearing its secure storage during the update. A fix is ready and will go out in the next app release, planned for Thursday.
- Export button: the designer will move it from the overflow menu to the toolbar on screens narrower than 600 pixels and share a mock-up by Wednesday.
- The support representative will update the help-centre article once the new layout ships.

Next, the team reviewed the on-call week. There were four pages, all outside office hours. Two were caused by a disk filling up on a log server; log rotation has since been fixed. One was a false alarm from a health check that was too strict, and its threshold was relaxed. The last was a real outage of eleven minutes when a configuration change removed a database permission. The team agreed that configuration changes to production must now go through the same review as code, and that the change log should record who approved each change.

Finally, the team discussed the planning for the next quarter. The main themes are reliability of notifications, a faster search page, and better tooling for support. Each engineer will write a one-page proposal for one theme before the next sync, including the problem, the measurement that shows it, the proposed change, and how we will know it worked.

Action items: backend engineers own the security email queue; the frontend engineer owns the mobile release; the designer owns the export button mock-up; the team lead updates the change review process; everyone writes a planning proposal.
"""


# ---------------------------------------------------------------------------
# Ⓒ 短文档
# ---------------------------------------------------------------------------

_NOTICE_MD = """\
# 关于国庆节放假安排的通知

各部门:

根据国家有关规定,结合公司实际情况,现将国庆节放假安排通知如下:十月一日至十月七日放假,共七天。九月二十八日(星期日)、十月十一日(星期六)正常上班。

放假前,请各部门做好安全检查,关闭不使用的电器和门窗。假期内如有紧急事务,请联系值班人员。

行政部
"""

_FAQ_TXT = """\
问:电子发票开具后多久可以收到?
答:订单完成后,电子发票会在一至三个工作日内发送到下单时填写的邮箱。如果没有收到,请先检查垃圾邮件文件夹,或在「我的订单」页面点击「重新发送发票」。
"""

_ONE_PAGER_MD = """\
# Guest Wi-Fi

Visitors can use the network named Guest-Visitors. The password changes every Monday and is printed on the card at reception.

The guest network is separate from the office network and only allows web browsing and video calls. It cannot reach printers or internal tools. Each device can stay connected for up to eight hours a day.

For problems, ask reception or email the IT help desk.
"""


#: Ⓐ, many short headed entries (a FAQ list): each entry is a heading and one
#: line — short sections that join their neighbours (B-206 K-3).
_FAQ_ENTRIES_MD = """\
# 会员服务常见问题

## 会员卡丢了怎么办？

在小程序「我的」页面挂失后补办，补办免费。

## 积分多久过期？

积分自获得之日起两年内有效，到期自动清零。

## 可以把积分转给家人吗？

可以，同一家庭账户内的成员之间可以互转积分。

## 生日当月有什么优惠？

生日当月消费享受双倍积分，另送一张五十元券。

## 会员等级怎么升级？

一年内累计消费满五千元自动升级为金卡会员。

## 金卡会员有哪些权益？

金卡会员享受免费停车两小时和专属客服热线。

## 退货后积分会扣回吗？

会，退货成功后对应订单获得的积分会被扣回。

## 优惠券可以叠加使用吗？

同一订单只能使用一张优惠券，不能叠加。

## 线上和门店积分通用吗？

通用，线上商城和全国门店的积分实时同步。

## 忘记登录密码怎么办？

在登录页点击「忘记密码」，用绑定手机号重置。

## 可以修改绑定的手机号吗？

可以，在账户设置里验证旧手机号后更换。

## 会员卡可以注销吗？

可以，联系客服申请注销，注销后积分作废。

## 积分可以换现金吗？

不可以，积分只能兑换商城里的礼品和优惠券。

## 兑换的礼品多久发货？

礼品兑换成功后三个工作日内发出。

## 门店营业时间是几点？

全国门店每天上午十点到晚上十点营业。

## 会员活动在哪里看？

会员活动每月一号在小程序首页的公告栏发布。
"""

#: Ⓐ, slide titles read as headings (100~180 characters, B-200 regression):
#: longer than the breadcrumb has room for, so their text goes into a body.
_SLIDE_TITLES_MD = """\
# 渠道大会演示文稿

## 二〇二六年度渠道合作伙伴大会：新一代智能办公产品线（含会议室解决方案、移动办公套件与数据安全组件）的整体规划、分阶段上市节奏、区域渠道激励政策与售后服务体系建设总览（第一部分：产品线全景与核心卖点）

本部分介绍三条产品线。每条产品线都有入门、标准、高级三个版本。

## 二〇二六年度渠道合作伙伴大会：新一代智能办公产品线（含会议室解决方案、移动办公套件与数据安全组件）的整体规划、分阶段上市节奏、区域渠道激励政策与售后服务体系建设总览（第二部分：分阶段上市节奏与备货建议）

第一阶段在三月上市，第二阶段在七月上市。建议渠道提前一个月备货。

## 二〇二六年度渠道合作伙伴大会：新一代智能办公产品线（含会议室解决方案、移动办公套件与数据安全组件）的整体规划、分阶段上市节奏、区域渠道激励政策与售后服务体系建设总览（第三部分：区域渠道激励与返点结算规则）

返点按季度结算，季度销售额超过目标的部分按百分之五返还。
"""


# ---------------------------------------------------------------------------
# Ⓓ 表格
# ---------------------------------------------------------------------------

_PRICE_ITEMS = (
    ("办公椅", ("标准款", "人体工学款", "网面款", "会议款"), 299.0),
    ("升降桌", ("1.2 米", "1.4 米", "1.6 米"), 1299.0),
    ("文件柜", ("三层", "四层", "带锁五层"), 459.5),
    ("白板", ("90×60 厘米", "120×90 厘米", "移动支架款"), 189.9),
    ("投影仪", ("便携款", "会议室款"), 2599.0),
    ("显示器支架", ("单屏", "双屏"), 159.0),
    ("台灯", ("护眼款", "充电款", "夹子款"), 89.5),
    ("碎纸机", ("小型", "办公型", "大容量"), 699.0),
    ("打印纸", ("A4 70 克 5 包", "A4 80 克 5 包", "A3 80 克 5 包"), 129.0),
    ("书架", ("三层", "五层"), 239.0),
    ("储物柜", ("六门", "九门", "十二门"), 899.0),
    ("会议桌", ("2.4 米", "3.0 米", "3.6 米"), 1899.0),
    ("沙发", ("单人位", "双人位", "三人位"), 1599.0),
    ("茶水柜", ("1.0 米", "1.2 米"), 659.0),
    ("屏风隔断", ("两人位", "四人位", "六人位"), 1150.0),
)
_PRICE_SCENES = ("开放办公区", "会议室", "前台接待区", "管理层办公室", "培训教室")


def _price_rows() -> list[list[str]]:
    rows: list[list[str]] = []
    for item_index, (name, specs, base) in enumerate(_PRICE_ITEMS):
        for spec_index, spec in enumerate(specs):
            n = len(rows) + 1
            price = base * (1 + 0.15 * spec_index)
            scene = _PRICE_SCENES[(item_index + spec_index) % len(_PRICE_SCENES)]
            rows.append([f"P{n:03d}", f"{name} {spec}", f"{price:.2f}", scene])
    return rows


def _csv_doc(
    form: str, slug: str, filename: str, header: list[str], rows: list[list[str]]
) -> FormDoc:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    writer.writerows(rows)
    units = tuple(cell for row in [header, *rows] for cell in row if cell)
    return FormDoc(form, slug, filename, buffer.getvalue().encode("utf-8"), units)


_STOCK_MATERIALS = (
    "螺丝 M4×12",
    "螺丝 M5×16",
    "垫片 4 毫米",
    "垫片 5 毫米",
    "扎带 200 毫米",
    "扎带 300 毫米",
    "网线 3 米",
    "网线 5 米",
    "电源线 1.8 米",
    "插线板 六位",
    "标签纸 40×30",
    "收纳盒 小号",
    "收纳盒 大号",
    "封箱胶带",
    "防静电手环",
    "清洁湿巾",
    "气泡袋 20×30",
    "纸箱 三号",
    "纸箱 五号",
    "手套 均码",
)


def _stock_sheet(warehouse: int) -> list[list[str | int]]:
    rows: list[list[str | int]] = []
    for index, material in enumerate(_STOCK_MATERIALS):
        quantity = (index * 37 + warehouse * 101) % 900 + 20
        rows.append(
            [
                f"W{warehouse}-{index + 1:03d}",
                material,
                quantity,
                50 + (index % 4) * 25,
                f"{warehouse} 号库 {index // 5 + 1} 排 {index % 5 + 1} 层",
            ]
        )
    return rows


_STOCK_HEADER = ["物料编码", "物料名称", "库存数量", "安全库存", "存放位置"]


def _xlsx_doc(form: str, slug: str, filename: str) -> FormDoc:
    """Two sheets of plain cells (no empty cells, so pandas keeps integers integral)."""
    workbook = Workbook()
    units: list[str] = []
    for warehouse in (1, 2):
        sheet = workbook.active if warehouse == 1 else workbook.create_sheet()
        assert sheet is not None
        sheet.title = f"{warehouse} 号库"
        units.append(sheet.title)
        sheet.append(_STOCK_HEADER)
        units.extend(_STOCK_HEADER)
        for row in _stock_sheet(warehouse):
            sheet.append(row)
            units.extend(str(cell) for cell in row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return FormDoc(form, slug, filename, buffer.getvalue(), tuple(units))


_ENGLISH_ITEMS = (
    ("Notebook", ("A5 ruled", "A5 dotted", "A4 ruled", "A4 squared")),
    ("Ballpoint pen", ("blue, box of 12", "black, box of 12", "red, box of 12")),
    ("Stapler", ("desktop", "long reach", "heavy duty")),
    ("Sticky notes", ("76x76 mm, 12 pads", "38x51 mm, 24 pads")),
    ("Folder", ("A4 ring binder", "A4 lever arch", "presentation folder")),
    ("Whiteboard marker", ("assorted, pack of 4", "black, pack of 10")),
    ("Desk organiser", ("mesh, 5 compartments", "wooden, 3 drawers")),
    (
        "Label printer tape",
        ("12 mm black on white", "18 mm black on white", "24 mm black on yellow"),
    ),
    ("USB-C cable", ("1 m", "2 m")),
    ("Wireless mouse", ("standard", "ergonomic", "travel")),
    ("Keyboard", ("full size", "compact", "ergonomic split")),
    ("Headset", ("mono, USB", "stereo, USB", "wireless with dongle")),
    ("Laptop stand", ("fixed", "adjustable")),
    ("Cable tray", ("under-desk, 60 cm", "under-desk, 120 cm")),
)


def _english_rows() -> list[list[str]]:
    rows: list[list[str]] = []
    for item_index, (item, variants) in enumerate(_ENGLISH_ITEMS):
        for variant_index, variant in enumerate(variants):
            n = len(rows) + 1
            price = 2.35 + item_index * 3.1 + variant_index * 1.45
            unit = "pack" if "pack" in variant or "box" in variant else "each"
            note = "ships in 2 days" if (item_index + variant_index) % 3 else "made to order"
            rows.append([f"SKU-{n:04d}", f"{item}, {variant}", unit, f"{price:.2f}", note])
    return rows


# ---------------------------------------------------------------------------
# Ⓔ 对话体(一行一句)
# ---------------------------------------------------------------------------

_SUPPORT_CHAT_TXT = """\
客服：您好，这里是售后服务中心，我是客服小周，请问有什么可以帮您？
客户：你好，我上周买的台灯今天到了，但是打开发现灯罩有一道裂痕。
客服：非常抱歉给您带来不好的体验。麻烦您提供一下订单号，我帮您查一下。
客户：订单号是 A20260315-0427。
客服：好的，请稍等。
客服：查到了，您购买的是护眼台灯白色款，三月十五日下单，三月十九日签收，对吗？
客户：对的。
客服：请问裂痕是在灯罩的哪个位置？方便的话可以拍一张照片发给我吗？
客户：在灯罩边缘，大概两厘米长。照片我发过去了。
客服：收到照片了，确实是运输过程中造成的损坏。实在不好意思。
客户：那现在怎么处理？换货还是退货？
客服：两种方式都可以，看您的需要。换货的话我们会给您寄一台全新的，退货的话款项会原路退回。
客户：换货要多久？
客服：换货的新品会在一个工作日内发出，一般两到三天送达。旧的台灯由快递上门取件，您不需要另外付运费。
客户：上门取件需要我在家吗？
客服：需要的，快递员会提前打电话和您约时间。您也可以选择放在小区的快递柜，我们把取件码发给您。
客户：放快递柜吧，白天家里没人。
客服：好的。那我帮您登记换货，取件方式选择快递柜。
客户：新的会不会也有问题？
客服：您放心，发货前仓库会开箱检查，并且会加一层泡沫包装。如果收到后还有问题，可以随时联系我们。
客户：好的，那就换货吧。
客服：已经帮您提交换货申请了，申请编号是 H0415-0832。稍后您会收到短信通知。
客户：收到了。旧台灯的配件要一起寄回去吗？
客服：需要的，台灯本体、底座、电源适配器和说明书请一起放回原包装。原包装如果丢了也没关系，用其他纸箱包好就行。
客户：说明书好像找不到了。
客服：说明书缺失不影响换货，其他配件齐全就可以。
客户：明白了。还有一个问题，这个台灯的保修期是多久？
客服：整灯保修一年，光源保修三年。换货后的新品保修期从您签收新品之日起重新计算。
客户：好的，那发票需要重新开吗？
客服：换货不需要重新开发票，原发票继续有效。如果您需要，我们也可以帮您把发票上的商品型号备注一下。
客户：不用了，谢谢。
客服：不客气。取件码会在明天上午发送到您的手机上，请注意查收。
客户：如果明天没收到取件码怎么办？
客服：如果明天中午十二点前还没收到，您可以直接回复这个对话，或者拨打售后电话，我们会帮您重新发送。
客户：好的。
客服：另外，为了表示歉意，我们给您的账户发放了一张二十元的无门槛优惠券，有效期三十天。
客户：谢谢，挺周到的。
客服：应该的。请问还有其他可以帮您的吗？
客户：没有了。
客服：好的，祝您生活愉快！稍后会有一条满意度评价短信，麻烦您抽空评价一下。
客户：好的，再见。
客服：再见。
"""

_SALES_CHAT_TXT = """\
[2026-04-02 10:02] 销售 小林：您好，我是负责您所在区域的销售顾问小林。看到您在官网留言咨询团队版的价格，现在方便聊几句吗？
[2026-04-02 10:05] 客户：可以。我们是一个四十人左右的设计团队，想找一个能管理项目进度和文件版本的工具。
[2026-04-02 10:06] 销售 小林：明白。团队版支持项目看板、任务分配、文件版本记录和在线批注，比较适合设计团队。请问你们现在用的是什么工具？
[2026-04-02 10:09] 客户：现在主要用共享网盘加表格，文件版本经常搞混，找一个旧版本要花很久。
[2026-04-02 10:10] 销售 小林：这个问题团队版可以解决。每次上传同名文件都会自动保存为新版本，旧版本保留一百八十天，随时可以对比和恢复。
[2026-04-02 10:12] 客户：价格是怎么算的？
[2026-04-02 10:13] 销售 小林：团队版按人数计费，每人每月六十元，按年付款可以打八五折。四十人按年付的话，一年是两万四千四百八十元。
[2026-04-02 10:15] 客户：存储空间有限制吗？我们的设计文件都比较大。
[2026-04-02 10:16] 销售 小林：团队版每人有一百 GB 空间，全团队共享使用。四十人就是四 TB，单个文件最大支持五 GB。
[2026-04-02 10:18] 客户：够用了。可以先试用吗？
[2026-04-02 10:18] 销售 小林：可以的，团队版提供三十天免费试用，人数不限，功能和正式版完全一样。试用期结束后数据会保留三十天，转正式版后可以直接继续使用。
[2026-04-02 10:21] 客户：数据可以从网盘迁移过来吗？
[2026-04-02 10:22] 销售 小林：可以。我们提供迁移工具，可以按文件夹批量导入，并保留原来的目录结构。如果文件量比较大，我们的实施同事可以远程协助迁移。
[2026-04-02 10:25] 客户：远程协助要另外收费吗？
[2026-04-02 10:25] 销售 小林：按年购买团队版的客户，首次迁移协助是免费的。
[2026-04-02 10:27] 客户：好的。我们还需要和外部客户共享部分文件，这个支持吗？
[2026-04-02 10:28] 销售 小林：支持外部分享链接，可以设置有效期、访问密码和是否允许下载。外部客户查看文件不需要注册账号。
[2026-04-02 10:30] 客户：权限能细到每个项目吗？
[2026-04-02 10:31] 销售 小林：能的。每个项目可以单独设置成员和角色，角色分为管理员、编辑者和查看者，查看者不能下载原始文件。
[2026-04-02 10:34] 客户：听起来不错。我需要和负责人商量一下，下周给你答复。
[2026-04-02 10:35] 销售 小林：没问题。我先给您开通试用账号，邀请链接会发到您留的邮箱。试用期间有任何问题都可以直接联系我。
[2026-04-02 10:36] 客户：好的，谢谢。
[2026-04-09 15:40] 客户：小林你好，我们试用了一周，整体挺满意的。负责人想问一下，如果人数在年中增加，费用怎么算？
[2026-04-09 15:42] 销售 小林：年中增加的人数按剩余月份折算，比如七月增加五个人，就按剩余六个月收费，同样享受八五折。
[2026-04-09 15:45] 客户：那如果人数减少呢？
[2026-04-09 15:46] 销售 小林：年付期间人数减少不退费，可以在续费时按实际人数调整。
[2026-04-09 15:48] 客户：明白了。合同和发票怎么走？
[2026-04-09 15:50] 销售 小林：我们可以签电子合同，也可以邮寄纸质合同。付款后三个工作日内开具增值税专用发票。我先把合同模板和报价单发您邮箱，您这边确认一下开票信息。
[2026-04-09 15:52] 客户：好的，收到后我转给财务。
[2026-04-09 15:53] 销售 小林：好的，期待合作！
"""

_ENGLISH_CHAT_TXT = """\
Agent: Hi, thanks for contacting support. My name is Sam. How can I help you today?
Customer: Hi Sam. I can't log in to my account since this morning. It says my password is wrong, but I'm sure it's right.
Agent: Sorry to hear that. Let's sort it out. Could you tell me the email address on the account?
Customer: It's the one I'm writing from.
Agent: Thanks. I can see the account. There were five failed login attempts at 7:42 this morning, so the account was locked for security.
Customer: That wasn't me. I was asleep at that time.
Agent: Understood. That's why the lock exists. Nothing was changed on the account, and no one got in.
Customer: OK, good. How do I get back in?
Agent: I'll send you a password reset link. It works once and expires after thirty minutes. Please choose a new password you haven't used before.
Customer: Got it. The email just arrived.
Agent: Great. Let me know when you're done.
Customer: Done. I'm in now.
Agent: Perfect. While you're there, I'd recommend turning on two-step verification. It's under Settings, then Security.
Customer: Is that the code from a text message?
Agent: You can choose a text message or an authenticator app. The app is a bit safer, because text messages can be redirected.
Customer: I'll use the app then. Which one do you recommend?
Agent: Any standard authenticator app works. Scan the QR code shown on the screen, then type the six-digit code to confirm.
Customer: It's asking for a code now. OK, it accepted it.
Agent: Well done. The page should now show ten backup codes. Please save them somewhere safe, offline if possible.
Customer: Why do I need those?
Agent: If you lose your phone, a backup code lets you log in once. Each code can be used only one time.
Customer: Makes sense. I printed them.
Agent: Perfect. One more thing: was the old password used on any other website?
Customer: Hmm, maybe on one or two.
Agent: Then I'd suggest changing it there too. Someone may have found it in a leak from another site, which is a common reason for attempts like this morning's.
Customer: I'll do that tonight.
Agent: Great. I've also added a note to your account, so if you contact us again about this, the next agent will see the history.
Customer: Thanks. Will I get an email if someone tries again?
Agent: Yes. You'll get an email after three failed attempts, and the account locks again after five. You can also see recent sign-ins under Settings, then Security, then Activity.
Customer: Found it. I can see this morning's attempts. They came from a different country.
Agent: That matches what I see. You can click Report next to each one, which helps our security team block that source.
Customer: Reported both.
Agent: Thank you. Is there anything else I can help you with today?
Customer: No, that's everything. You've been very helpful.
Agent: Glad to help. Have a good day, and stay safe online.
Customer: You too. Bye.
"""


# ---------------------------------------------------------------------------
# Ⓕ 扫描件 / 图片
# ---------------------------------------------------------------------------


def _gray_png(size: int = 64) -> bytes:
    """A blank grey square — an image with no text in it."""
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, size, size), False)
    pixmap.set_rect(pixmap.irect, (200, 200, 200))
    return bytes(pixmap.tobytes("png"))


def _photo_png(width: int = 640, height: int = 480, seed: int = 3) -> bytes:
    """A picture big enough to describe: noisy pixels never compress below
    the figure filters' 10 KB floor (a photo, as far as the filters can tell)."""
    pixels = hashlib.shake_256(f"photo-{seed}".encode()).digest(width * height * 3)
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, width, height, pixels, False)
    return bytes(pixmap.tobytes("png"))


def _scanned_pdf(pages: int = 2) -> bytes:
    """A PDF whose pages are images only — no text layer, as a scanner produces."""
    document = pymupdf.open()
    for _ in range(pages):
        page = document.new_page()
        page.insert_image(pymupdf.Rect(36, 36, 559, 806), stream=_gray_png())
    return bytes(document.tobytes())


_MIXED_TEXT_PAGES = (
    ("第一章 总则", "本手册适用于全体员工,自发布之日起施行。"),
    ("第三章 考勤", "员工请假须提前三个工作日在系统中提交申请,由直属主管审批。"),
)


def _mixed_pdf() -> bytes:
    """B-209 —— text page, scanned page, text page: a handbook whose middle
    page was scanned in (the PDF form the corpus has 50 of). Before B-209 its
    scanned page was silently empty (``_is_sparse`` averaged the whole file)."""
    document = pymupdf.open()
    for number, lines in enumerate(_MIXED_TEXT_PAGES):
        page = document.new_page()
        for row, line in enumerate(lines):
            page.insert_text((72, 72 + 24 * row), line, fontname="china-s", fontsize=12)
        if number == 0:
            scan = document.new_page()
            scan.insert_image(pymupdf.Rect(36, 36, 559, 806), stream=_gray_png())
    return bytes(document.tobytes())


_DOCX_DRAWING = (
    "<w:p><w:r><w:drawing>"
    '<wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
    '<wp:extent cx="5486400" cy="7315200"/><wp:docPr id="1" name="scan"/>'
    '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
    '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
    '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
    '<pic:nvPicPr><pic:cNvPr id="1" name="scan.png"/><pic:cNvPicPr/></pic:nvPicPr>'
    f'<pic:blipFill><a:blip xmlns:r="{_R_NS}" r:embed="rId2"/></pic:blipFill>'
    '<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="5486400" cy="7315200"/></a:xfrm>'
    '<a:prstGeom prst="rect"/></pic:spPr></pic:pic></a:graphicData></a:graphic>'
    "</wp:inline></w:drawing></w:r></w:p>"
)


def picture_docx(image: bytes, *, before: str = "", after: str = "", repeat: int = 1) -> bytes:
    """B-209 PR-2 —— a Word file with ``image`` (PNG) placed ``repeat`` times
    between the paragraphs ``before`` and ``after`` (each left out when empty)."""
    body = "".join(
        [
            _docx_paragraph(before, None) if before else "",
            _DOCX_DRAWING * repeat,
            _docx_paragraph(after, None) if after else "",
        ]
    )
    return _docx(body, image=image)


def _scanned_docx() -> bytes:
    """A Word file holding one picture and no text (a scan pasted into Word)."""
    return picture_docx(_gray_png())


def _placeable_wmf() -> bytes:
    """A minimal placeable Windows Metafile (header + end record): a vector
    picture format slides still carry, which nothing here can rasterise."""
    head = b"\xd7\xcd\xc6\x9a" + struct.pack("<H4hHI", 0, 0, 0, 1000, 1000, 1440, 0)
    checksum = 0
    for (word,) in struct.iter_unpack("<H", head):
        checksum ^= word
    body = struct.pack("<HHHIHIH", 1, 9, 0x300, 12, 0, 3, 0) + struct.pack("<IH", 3, 0)
    return head + struct.pack("<H", checksum) + body


def picture_pptx(image: bytes, *, title: str = "", extra_wmf: bool = False) -> bytes:
    """B-209 PR-2 —— a one-slide deck holding ``image`` (and, with
    ``extra_wmf``, a WMF picture too); ``title`` adds a text box above it."""
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    if title:
        slide.shapes.add_textbox(Inches(0.5), Inches(0.2), Inches(9), Inches(0.6)).text = title
    slide.shapes.add_picture(io.BytesIO(image), Inches(0.5), Inches(1), Inches(9), Inches(5.5))
    if extra_wmf:
        slide.shapes.add_picture(
            io.BytesIO(_placeable_wmf()), Inches(7), Inches(6), Inches(2), Inches(1)
        )
    buffer = io.BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _scanned_pptx() -> bytes:
    """A slide deck whose only slide is one picture."""
    return picture_pptx(_gray_png())


# ---------------------------------------------------------------------------
# 测试集
# ---------------------------------------------------------------------------

#: Ⓐ ~ Ⓔ — documents with text.
TEXT_FORM_DOCS: tuple[FormDoc, ...] = (
    _text_doc("A", "handbook-zh-md", "员工手册.md", _HANDBOOK_MD),
    FormDoc(
        "A",
        "contract-zh-docx",
        "软件服务合同.docx",
        _docx("".join(_docx_paragraph(text, style) for style, text in _CONTRACT_PARAGRAPHS)),
        tuple(text for _, text in _CONTRACT_PARAGRAPHS),
    ),
    _text_doc("A", "expense-policy-en-md", "travel_expense_policy.md", _ENGLISH_POLICY_MD),
    _text_doc("A", "faq-entries-zh-md", "会员服务常见问题.md", _FAQ_ENTRIES_MD),
    _text_doc("A", "slide-titles-zh-md", "渠道大会演示文稿.md", _SLIDE_TITLES_MD),
    _text_doc("B", "meeting-notes-zh-txt", "项目周会纪要.txt", _MEETING_NOTES_TXT),
    _text_doc("B", "worklog-export-zh-txt", "值班日志导出.txt", _WORKLOG_EXPORT_TXT),
    _text_doc("B", "sync-notes-en-md", "weekly_sync_notes.md", _ENGLISH_NOTES_MD),
    _text_doc("C", "holiday-notice-zh-md", "放假通知.md", _NOTICE_MD),
    _text_doc("C", "invoice-faq-zh-txt", "发票常见问题.txt", _FAQ_TXT),
    _text_doc("C", "guest-wifi-en-md", "guest_wifi.md", _ONE_PAGER_MD),
    _csv_doc(
        "D",
        "furniture-prices-zh-csv",
        "办公家具价目表.csv",
        ["编号", "产品名称", "单价(元)", "推荐场景"],
        _price_rows(),
    ),
    _xlsx_doc("D", "stock-zh-xlsx", "耗材库存表.xlsx"),
    _csv_doc(
        "D",
        "stationery-prices-en-csv",
        "stationery_price_list.csv",
        ["SKU", "Item", "Unit", "Price (USD)", "Notes"],
        _english_rows(),
    ),
    _text_doc("E", "support-chat-zh-txt", "售后客服对话.txt", _SUPPORT_CHAT_TXT),
    _text_doc("E", "sales-chat-zh-txt", "销售沟通记录.txt", _SALES_CHAT_TXT),
    _text_doc("E", "support-chat-en-txt", "support_chat.txt", _ENGLISH_CHAT_TXT),
)

#: Ⓕ — no text layer. ``units`` is empty: nothing a user wrote is in them.
SCANNED_DOCS: tuple[FormDoc, ...] = (
    FormDoc("F", "scan-pdf", "合同扫描件.pdf", _scanned_pdf(), ()),
    FormDoc("F", "scan-docx", "合同扫描件.docx", _scanned_docx(), ()),
    FormDoc("F", "scan-pptx", "培训材料扫描件.pptx", _scanned_pptx(), ()),
    FormDoc("F", "photo-png", "发票照片.png", _gray_png(), ()),
)

#: Ⓕ (B-209 PR-2) — a Word file / slide deck that is one picture big enough to
#: describe: with a figure describer it is searchable by the description.
PICTURE_DOCS: tuple[FormDoc, ...] = (
    FormDoc("F", "picture-docx", "设备外观图.docx", picture_docx(_photo_png()), ()),
    FormDoc("F", "picture-pptx", "设备外观图.pptx", picture_pptx(_photo_png()), ()),
)

#: Ⓕ + text (B-209) — a PDF mixing text pages and a scanned page. ``units``
#: are the text pages' lines; the scanned page holds none a parser can see.
MIXED_DOCS: tuple[FormDoc, ...] = (
    FormDoc(
        "F",
        "mixed-pdf",
        "员工手册部分扫描.pdf",
        _mixed_pdf(),
        tuple(line for lines in _MIXED_TEXT_PAGES for line in lines),
    ),
)
