"""
生成《FF14 材料台账 · 新手引导》PPT。

用法：python tools\make_guide_pptx.py
或双击项目根目录的「生成新手引导.bat」（会自动装 python-pptx）。

输出：项目根目录 / FF14材料台账-新手引导.pptx
"""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Emu, Inches, Pt
except ImportError:
    print("缺少 python-pptx，请先运行：python -m pip install python-pptx")
    sys.exit(1)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "FF14材料台账-新手引导.pptx"

W = Inches(13.333)
H = Inches(7.5)

DARK = RGBColor(0x1F, 0x4E, 0x5F)
ACCENT = RGBColor(0x14, 0xA3, 0xB8)
TEXT = RGBColor(0x22, 0x31, 0x3A)
MUTED = RGBColor(0x6B, 0x7A, 0x85)
LIGHT = RGBColor(0xF2, 0xF6, 0xF8)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
WARN = RGBColor(0xB4, 0x53, 0x09)

prs = Presentation()
prs.slide_width = W
prs.slide_height = H
BLANK = prs.slide_layouts[6]

page_counter = {"n": 0}


def new_slide():
    page_counter["n"] += 1
    return prs.slides.add_slide(BLANK)


def rect(slide, left, top, width, height, color):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape


def textbox(slide, left, top, width, height, text, size=20, bold=False,
            color=TEXT, align=PP_ALIGN.LEFT, line_spacing=1.4):
    box = slide.shapes.add_textbox(left, top, width, height)
    frame = box.text_frame
    frame.word_wrap = True
    lines = text.split("\n")
    for idx, line in enumerate(lines):
        para = frame.paragraphs[0] if idx == 0 else frame.add_paragraph()
        para.alignment = align
        para.line_spacing = line_spacing
        run = para.add_run()
        run.text = line
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = color
        run.font.name = "微软雅黑"
    return box


def footer(slide):
    textbox(slide, Inches(0.8), Inches(6.95), Inches(6), Inches(0.35),
            "FF14 材料台账 · 新手引导", size=11, color=MUTED)
    textbox(slide, Inches(11.4), Inches(6.95), Inches(1.2), Inches(0.35),
            str(page_counter["n"]), size=11, color=MUTED, align=PP_ALIGN.RIGHT)


def cover():
    slide = new_slide()
    rect(slide, 0, 0, W, H, DARK)
    rect(slide, 0, Inches(4.9), W, Inches(0.09), ACCENT)
    textbox(slide, Inches(1.1), Inches(2.3), Inches(11), Inches(1.2),
            "FF14 材料台账", size=54, bold=True, color=WHITE)
    textbox(slide, Inches(1.1), Inches(3.6), Inches(11), Inches(0.8),
            "新手引导 · 从零开始把材料记清楚", size=26, color=RGBColor(0x9F, 0xD8, 0xE3))
    textbox(slide, Inches(1.1), Inches(5.3), Inches(11), Inches(0.6),
            "多角色、多雇员、多账号的材料，一处记录、一处查询", size=16,
            color=RGBColor(0xC9, 0xD6, 0xDD))
    textbox(slide, Inches(1.1), Inches(6.2), Inches(11), Inches(0.5),
            "v0.7.0", size=13, color=RGBColor(0x8F, 0xA6, 0xB0))


def section(num: str, title: str, subtitle: str = ""):
    slide = new_slide()
    rect(slide, 0, 0, W, H, LIGHT)
    rect(slide, Inches(0.9), Inches(2.9), Inches(0.14), Inches(1.4), ACCENT)
    textbox(slide, Inches(1.3), Inches(2.75), Inches(2), Inches(0.6),
            num, size=20, bold=True, color=ACCENT)
    textbox(slide, Inches(1.3), Inches(3.15), Inches(10.5), Inches(0.9),
            title, size=36, bold=True, color=DARK)
    if subtitle:
        textbox(slide, Inches(1.3), Inches(4.15), Inches(10.5), Inches(0.6),
                subtitle, size=18, color=MUTED)
    footer(slide)


def content(title: str, items: list, note: str = "", note_color=MUTED):
    slide = new_slide()
    rect(slide, 0, 0, W, Inches(0.13), ACCENT)
    textbox(slide, Inches(0.8), Inches(0.45), Inches(11.7), Inches(0.8),
            title, size=32, bold=True, color=DARK)
    rect(slide, Inches(0.85), Inches(1.22), Inches(1.1), Inches(0.06), ACCENT)

    box = slide.shapes.add_textbox(Inches(0.9), Inches(1.55), Inches(11.6), Inches(4.7))
    frame = box.text_frame
    frame.word_wrap = True
    for idx, item in enumerate(items):
        para = frame.paragraphs[0] if idx == 0 else frame.add_paragraph()
        para.line_spacing = 1.5
        para.space_after = Pt(10)
        run = para.add_run()
        run.text = "·  " + item
        run.font.size = Pt(19)
        run.font.color.rgb = TEXT
        run.font.name = "微软雅黑"

    if note:
        rect(slide, Inches(0.9), Inches(6.05), Inches(11.6), Inches(0.62), LIGHT)
        textbox(slide, Inches(1.1), Inches(6.13), Inches(11.2), Inches(0.5),
                note, size=13, color=note_color)
    footer(slide)
    return slide


def steps(title: str, pairs: list, note: str = ""):
    slide = new_slide()
    rect(slide, 0, 0, W, Inches(0.13), ACCENT)
    textbox(slide, Inches(0.8), Inches(0.45), Inches(11.7), Inches(0.8),
            title, size=32, bold=True, color=DARK)
    rect(slide, Inches(0.85), Inches(1.22), Inches(1.1), Inches(0.06), ACCENT)

    top = Inches(1.65)
    for idx, (head, body) in enumerate(pairs):
        y = top + Emu(int(Inches(1.15)) * idx)
        circle = slide.shapes.add_shape(MSO_SHAPE.OVAL, Inches(0.95), y, Inches(0.52), Inches(0.52))
        circle.fill.solid()
        circle.fill.fore_color.rgb = ACCENT
        circle.line.fill.background()
        tf = circle.text_frame
        tf.text = str(idx + 1)
        para = tf.paragraphs[0]
        para.alignment = PP_ALIGN.CENTER
        para.runs[0].font.size = Pt(16)
        para.runs[0].font.bold = True
        para.runs[0].font.color.rgb = WHITE

        textbox(slide, Inches(1.7), y - Inches(0.04), Inches(10.6), Inches(0.44),
                head, size=20, bold=True, color=DARK)
        textbox(slide, Inches(1.7), y + Inches(0.38), Inches(10.6), Inches(0.5),
                body, size=15, color=MUTED)

    if note:
        rect(slide, Inches(0.9), Inches(6.35), Inches(11.6), Inches(0.55), LIGHT)
        textbox(slide, Inches(1.1), Inches(6.42), Inches(11.2), Inches(0.45),
                note, size=13, color=MUTED)
    footer(slide)
    return slide


def main() -> int:
    cover()

    section("01", "这是什么", "解决「材料散在多个角色和雇员身上，游戏里查不到」的问题")
    content("为什么会需要它", [
        "FF14 一个账号下的多个角色、多个雇员、部队柜、房屋，库存互不可见",
        "同账号同一时间只能登一个角色，想查「铁锭在哪」只能挨个登录翻",
        "多账号更麻烦：账号之间完全隔离，连邮寄都得一个个换",
        "靠脑子记迟早会错；靠 Excel 又难查、容易重复录入",
    ], note="这个工具做的是：把库存记在一个本地台账里，随时按名字查。")

    section("02", "三步上手", "添加角色 → 录入材料 → 搜索查询")
    steps("最快跑起来只需要三步", [
        ("添加角色", "一个角色就是一个仓库，先把你所有角色加进去"),
        ("录入材料", "选中角色 → 输入材料名（有联想）→ 填位置和数量 → 保存"),
        ("搜索查询", "在顶部搜索框输入材料名 / 角色名 / 位置，立刻看到分布"),
    ], note="第一次建仓材料多的话，用「批量粘贴录入」能一次导入几十条。")

    section("03", "第一次使用")
    steps("启动与初始化", [
        ("双击「启动.bat」", "黑窗口出现几行字，浏览器会自动打开 http://127.0.0.1:8765/"),
        ("第一次启动会准备物品表", "5 万条物品名，几秒钟完成；之后完全离线"),
        ("别关黑窗口", "关掉就等于退出程序；数据在关之前就已存盘，不会丢"),
    ], note="启动后页面顶部会显示版本号，和你手上的压缩包一致就说明没问题。")

    content("添加角色：一个角色 = 一个仓库", [
        "页面下方「＋ 添加角色」：填角色名，服务器可选，账号是备注（方便区分多账号）",
        "加错了可以直接删，不影响其他角色",
        "角色的意义：录入和查询都按「角色 + 位置」定位到具体格子",
    ], note="位置默认给了 背包 / 鞍囊 / 雇员1-5 / 部队柜 / 房屋，可以按自己的实际情况用。")

    content("录入一笔：三个要点", [
        "材料名必须能从物品库联想出来（打两个字就会弹出候选，↑↓ 选、Enter 确认）",
        "数量有三个框：总数 / 组 / 余，填哪个都行，另外两个自动算",
        "HQ 勾上表示高品质；同一格重复录入是「覆盖」不是「累加」",
    ], note="名字打错会被拦住，不会把错名字写进库里——这是有意设计的。")

    content("批量粘贴录入：建仓最快的方式", [
        "格式：材料名 数量 [位置] [hq]，一行一条，以 # 开头的是注释",
        "数量可以写 1524，也可以写 1组525",
        "位置省略时用左边选中的默认位置",
        "导完会逐行告诉你是成功、未变还是失败",
    ], note="注意：同一角色+位置+材料+品质写两行，只保留最后一行（覆盖不是累加）。",
            note_color=WARN)

    content("搜索：一个框搜所有", [
        "材料名、备注名、角色名、账号、服务器、位置，都能直接搜",
        "结果按「接近程度」排序：精确 > 前缀 > 结尾 > 包含",
        "搜「雇员1」能列出这个雇员里放了什么，不用先想起材料名",
    ], note="材料名右边的 #5057 是物品 ID，点一下复制，图标加载不出来时凭它去查。")

    content("备注名：让你自己的叫法也能搜到", [
        "在「名字映射」里给物品挂备注名，例如把「黑铁锭」备注成「铁锭」",
        "作用一：输入「铁锭」直接解析成「黑铁锭」",
        "作用二：搜「铁锭」能搜到黑铁锭的库存，结果标题会标「备注：铁锭」",
        "一个物品可以挂多个备注名；记录里存的永远是标准名",
    ])

    section("04", "日常使用")
    steps("取货：从某一格拿东西出来", [
        ("点那一行的「取」", "行下方展开三个输入框：总数 / 组 / 余"),
        ("填数量，Enter 或点确认", "右边实时显示换算结果和占几格"),
        ("减到 0 自动移除该行", "每次都会写流水，记录取出量和约等于几格"),
    ])

    content("修改与删除", [
        "「改」：把这一行的角色 / 位置 / 数量填回表单，改完保存即覆盖",
        "「删」：点一次变「确认删除」，3 秒内再点一次才真正删除",
        "数量没变化的保存不会产生任何记录（避免流水里堆满「覆盖 +0」）",
    ])

    content("审计：核对这笔账对不对", [
        "按材料汇总一段时间内的新增 / 取出 / 调整 / 删除 / 净变化",
        "底部流水可以逐条勾「已核对」，审计面板可只统计核对过的",
        "发现记错了：点「异常」写一句原因，这条会自动从汇总里排除",
        "异常只影响统计，不改库存——库存要用「改」去改",
    ], note="审计可以导出 CSV，带 BOM，Excel 直接打开不乱码。")

    content("快照：随时回到某个状态", [
        "每执行一次存入 / 取出 / 改 / 删，都会自动存一份快照，保留最近 30 个",
        "手动快照永久保留，适合在「建仓完成」「大整理前」打一个",
        "恢复前会自动把当前状态也存一份，所以恢复本身也能反悔",
        "快照可以「查看」（只读看那一刻的库存）和「下载」（另存成文件）",
    ])

    content("撤销与对账", [
        "「撤销上一步」：只撤销最近一条库存操作，点两次确认；撤销本身不能再撤销",
        "「对账」：逐格比对当前数量和流水累计，列出差多少",
        "对账全部对得上，说明这本账没有漏记错记",
    ], note="要回退更早的状态，用快照，不要靠撤销。")

    section("05", "数据与常见问题")
    content("数据存在哪、怎么备份", [
        "所有数据都在 data\\stock.db 一个文件里（快照在 data\\snapshots\\）",
        "备份 = 复制 data 目录；换机器 = 把 data 目录一起拷过去",
        "也可以用「导出库存 CSV」给别人看，但那不是完整备份（没有流水和备注名）",
    ], note="程序不会上传任何数据，全部存在你自己电脑上。")

    content("常见问题", [
        "图标不显示：需要联网，取不到只是没图，不影响数据",
        "名字搜不到：可能叫法不同，用「名字映射」加个备注名，或在「未收录」里绑定",
        "导出 CSV 打开是乱码：用 Excel 双击打开（文件带 BOM），别用记事本另存",
        "想换端口：启动.bat 里加 --port 9000",
    ])

    stack = new_slide()
    rect(slide=stack, left=0, top=0, width=W, height=H, color=DARK)
    textbox(stack, Inches(1.1), Inches(2.6), Inches(11), Inches(1),
            "现在就去加第一个角色吧", size=40, bold=True, color=WHITE)
    textbox(stack, Inches(1.1), Inches(3.8), Inches(11), Inches(0.8),
            "遇到问题先看 data\\error.log，再对照本手册的「常见问题」",
            size=18, color=RGBColor(0xC9, 0xD6, 0xDD))
    textbox(stack, Inches(1.1), Inches(5.4), Inches(11), Inches(0.6),
            "双击「启动.bat」→ http://127.0.0.1:8765/", size=16,
            color=RGBColor(0x9F, 0xD8, 0xE3))

    prs.save(OUT)
    print(f"已生成：{OUT}")
    print(f"共 {page_counter['n']} 页")
    return 0


if __name__ == "__main__":
    sys.exit(main())
