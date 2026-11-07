"""常用工作流（skill）：把一段固定套路变成一次点击。

skill 是**数据，不是代码**。一条 skill 就是「菜单文案 + 一句话适用场景 + 一段可
编辑的提示词」。点击后只把提示词填进输入框，用户还能改——这样它既能省掉反复
描述的口舌，又不会把人的意图锁死在模板里。

内置一批常用工作流；用户想加自己的，往 ``<workspace>/.gangge/skills/*.json``
丢一个文件即可（字段：id / label / template，可选 group / hint / focus）。

模板里的占位符用 ``＜……＞`` 包起来：GUI 填进输入框后会自动选中第一个占位符，
直接打字就能覆盖，不用手去删。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_GROUP = "我的"
USER_SKILL_DIR = Path(".gangge") / "skills"

#: 效果预览图放这儿（由 storyboard 的 ``_make_previews`` 生成，随包分发）。
#: 有图就在右侧「效果」页签里显示，没有就只是一句话说明——预览是锦上添花，
#: 缺图不能让技能点不动。
PREVIEW_DIR = Path(__file__).resolve().parent / "storyboard" / "previews"

#: 视频类模板共用的尾巴：镜头结构与口播要求写死，避免每次重复描述。
_VIDEO_TAIL = (
    "要求：4-6 个镜头；第一个是标题、最后一个是结尾；"
    "口播口语化，不用「赋能」「抓手」这类空话。"
)

#: (style, 视觉标签, 适用场景) —— 菜单里的「一句话提示」就是这里的 hint，
#: 也就是"没有示例也没有提示"这个问题的答案：风格选择自带场景说明。
_VIDEO_STYLES: tuple[tuple[str, str, str], ...] = (
    ("dark", "克制深色", "最不挑内容，投屏和手机都不刺眼（默认）"),
    ("neon", "发光科技", "AI / 硬件 / 游戏，要点少但要抓眼"),
    ("paper", "纸张书卷", "文化 / 历史 / 人物故事，语速会自然变慢"),
    ("glass", "玻璃拟态", "SaaS / 设计 / 品牌汇报，要「高级感」"),
    ("brutal", "撞色粗框", "设计 / 潮流 / 独立产品，记忆点最强"),
)


@dataclass(frozen=True)
class Skill:
    """一条可点击的工作流。"""

    id: str
    label: str
    template: str
    hint: str = ""
    group: str = DEFAULT_GROUP
    focus: str = ""  # 填进输入框后自动选中的占位符
    preview: str = ""  # 效果预览图文件名（相对 PREVIEW_DIR），空=无视觉预览

    @property
    def menu_text(self) -> str:
        return self.label

    @classmethod
    def from_dict(cls, data: dict) -> Skill:
        sid = str(data.get("id") or "").strip()
        label = str(data.get("label") or "").strip()
        template = str(data.get("template") or "").strip()
        if not sid or not label or not template:
            raise ValueError("skill 缺少 id / label / template")
        return cls(
            id=sid,
            label=label,
            template=template,
            hint=str(data.get("hint") or "").strip(),
            group=str(data.get("group") or DEFAULT_GROUP).strip() or DEFAULT_GROUP,
            focus=str(data.get("focus") or "").strip(),
            preview=str(data.get("preview") or "").strip(),
        )


def _video_templates() -> list[Skill]:
    skills: list[Skill] = []
    for style, tag, hint in _VIDEO_STYLES:
        skills.append(
            Skill(
                id=f"video_{style}",
                group="视频出片",
                label=f"30 秒讲解视频 · {style}",
                hint=f"{tag}：{hint}",
                focus="＜主题＞",
                preview=f"{style}.png",
                template=(
                    f"用 demo_video 工具做一条 30 秒左右的讲解短视频，style={style}。\n"
                    f"主题：＜主题＞\n{_VIDEO_TAIL}"
                ),
            )
        )
    skills.append(
        Skill(
            id="video_auto",
            group="视频出片",
            label="30 秒讲解视频 · 自动选风格",
            hint="不确定用哪套时选它：按内容气质自动挑",
            focus="＜主题＞",
            preview="dark.png",  # 兜底风格就是 dark，拿它当示例图
            template=(
                "用 demo_video 工具做一条 30 秒左右的讲解短视频。先按内容气质定 style，再调用工具：\n"
                "技术/产品 → dark 或 neon；文化/叙事 → paper；商业汇报 → glass；"
                "潮流/独立 → brutal；拿不准 → dark。\n"
                f"主题：＜主题＞\n{_VIDEO_TAIL}"
            ),
        )
    )
    skills.append(
        Skill(
            id="video_portrait",
            group="视频出片",
            label="竖屏短视频 · 9:16",
            hint="手机竖屏：字更大一行更少，一句一镜，直接发抖音/小红书",
            focus="＜主题＞",
            preview="portrait.png",
            template=(
                "用 demo_video 工具做一条 30 秒左右的竖屏短视频，aspect=9:16，style=neon。\n"
                "主题：＜主题＞\n"
                "口播：一镜一句，每句不超过 20 字，念出来要顺口；\n"
                "画面：开场一句话点题，中段每屏最多 3 条，结尾给一句行动号召。\n"
                "要求：5-6 个镜头；第一个是标题、最后一个是结尾；配音用默认音色。"
            ),
        )
    )
    return skills


def _page_templates() -> list[Skill]:
    """另外几种「页面」：3D 展示页、展示页里装真站点、人物光线、时间线飞行、迷你沙盘。

    和上面五种风格并列放在「视频出片」里——用户看到的都是"出一片"，只是拍法不同。
    关键词分别是 ``page=showcase`` / ``page=site`` / ``page=person`` / ``page=flight``
    / ``page=miniature``，模型照着抄就能命中工具的枚举。
    """
    return [
        Skill(
            id="showcase_3d",
            group="视频出片",
            label="3D 展示页视频 · 产品/项目介绍",
            hint="分镜装进浏览器框里，滑动就是推镜头；单个 HTML 发给别人也能看",
            focus="＜主题＞",
            preview="showcase.png",
            template=(
                "用 demo_video 工具做一条 30 秒左右的 3D 展示页视频，page=showcase，style=neon。\n"
                "主题：＜主题＞\n"
                f"{_VIDEO_TAIL}"
            ),
        ),
        Skill(
            id="style_studio",
            group="视频出片",
            label="样式工作台 · 风格与装饰随手调",
            hint="5 种风格 × 5 层装饰 × 横竖屏，自由组合看效果，选好再拍",
            template=(
                "运行 `python -m gangge.layer4_tools.storyboard.studio`，"
                "再把生成的 `.gangge/style-studio.html` 用浏览器打开。"
                "我要自己调风格、装饰和画幅——调好后按页面底部导出的配置拍片。"
            ),
        ),
        Skill(
            id="site_tour",
            group="视频出片",
            label="网站展示视频 · 真站点截图",
            hint="截目标网站整页，在 3D 框里从上往下推一遍；介绍自己的项目最直观",
            focus="＜网址＞",
            preview="site.png",
            template=(
                "用 demo_video 工具做一条展示网站的视频，page=site，style=neon。\n"
                "网址：＜网址＞\n"
                "要求：3-4 个镜头；开场一句话说清这个站点解决什么问题，"
                "结尾给一句行动号召；口播口语化，不用「赋能」「抓手」这类空话。"
            ),
        ),
        Skill(
            id="person_doc",
            group="视频出片",
            label="人物纪录片 · 一个人一生一条光路",
            hint="给一个人名或生平，光点沿线走过每个年份，停一站讲一段；照片可选",
            focus="＜人物＞",
            preview="person.png",
            template=(
                "用 demo_video 工具做一条人物纪录片（浮生光线），page=person，style=dark。\n"
                "人物：＜人物＞\n"
                "要求：7-10 个镜头。第一个用 template=title 当开场"
                "（title=人名，text=一句话概括这个人的一生）；"
                "中间每个镜头是一个人生节点，用 template=quote，"
                "year 写年份或年代（不确定就省略），title 一句话说清这一站，"
                "text 写 35-70 字讲发生了什么；最后用 template=outro 收一句总结。\n"
                "不要编造具体日期、数字和引语。\n"
                "照片：用户给了照片目录就传给 photos 参数；没给就直接出片，"
                "页面会画占位框，回头补图再渲染一次即可。"
            ),
        ),
        Skill(
            id="flight_timeline",
            group="视频出片",
            label="时间线飞行 · 3D 光路片头",
            hint="一条光路在夜色空间里延伸，镜头跟着飞，到站停住推近讲一段"
            "；公司史 / 项目大事记 / 传记片头这类有先后顺序的内容最合适",
            focus="＜主题＞",
            preview="flight.png",
            template=(
                "用 demo_video 工具做一条时间线飞行短片，page=flight，style=dark。\n"
                "主题：＜主题＞\n"
                "要求：7-10 个镜头。第一个用 template=title 当开场"
                "（title=标题，text=一句话概括这段历程）；"
                "中间每个镜头是一个时间节点，用 template=quote，"
                "year 写年份或年代（不确定就省略），title 一句话说清这一站（≤14字），"
                "text 写 35-70 字讲发生了什么；最后用 template=outro 收一句总结。\n"
                "不要编造具体日期、数字和引语。\n"
                "照片：用户给了照片目录就传给 photos 参数；没给就直接出片，"
                "站点旁边会留占位框，回头补图再渲染一次即可。"
            ),
        ),
        Skill(
            id="miniature_map",
            group="视频出片",
            label="迷你沙盘 · 白天治愈系 3D 地图",
            hint="小路从乡村木屋走到城市高楼，箭头一路前进、到站落下图钉"
            "；品牌故事 / 旅行回顾 / 成长历程，比夜景温柔",
            focus="＜主题＞",
            preview="miniature.png",
            template=(
                "用 demo_video 工具做一条迷你沙盘短片，page=miniature，style=dark"
                "（沙盘固定是白天的治愈配色，style 只影响文案语气）。\n"
                "主题：＜主题＞\n"
                "要求：5-8 个镜头。第一个用 template=title 当开场"
                "（title=标题，text=一句话概括这段路）；"
                "中间每个镜头是路上的一站，用 template=quote，"
                "title 一句话说清这一站（≤14字），text 写 35-70 字，"
                "最好带具体的地点或画面；最后用 template=outro 收一句总结。\n"
                "不要编造具体日期、数字和引语。\n"
                "照片：用户给了照片目录就传给 photos 参数；没给就直接出片。"
            ),
        ),
    ]


BUILTIN_SKILLS: tuple[Skill, ...] = (
    *_video_templates(),
    *_page_templates(),
    Skill(
        id="code_review",
        group="开发",
        label="审查当前改动",
        hint="只看 diff，按风险分级给结论，不动代码",
        template=(
            "只审查当前未提交的改动（git diff），不要改代码。\n"
            "按「必须改 / 建议改 / 可以不管」三档给结论，每条指到文件与行号。"
        ),
    ),
    Skill(
        id="add_tests",
        group="开发",
        label="给模块补测试",
        hint="先读实现再补用例，跑通再报完成",
        focus="＜模块路径＞",
        template=(
            "给 ＜模块路径＞ 补单元测试：先读实现，覆盖正常路径和一个边界情况；\n"
            "只新增/修改测试文件，不动业务代码；最后运行 pytest 确认通过再报完成。"
        ),
    ),
    Skill(
        id="explain",
        group="开发",
        label="解释模块 / 目录",
        hint="读代码后讲清职责与数据流，指到具体文件",
        focus="＜模块或目录＞",
        template=(
            "解释 ＜模块或目录＞ 的职责、关键类和数据流，200 字以内，"
            "每个结论都指到具体文件与行号。"
        ),
    ),
    Skill(
        id="refactor",
        group="开发",
        label="批量重命名 / 迁移",
        hint="机械改造，建议外包给 qoder 省上下文",
        focus="＜旧名＞",
        template=(
            "把 ＜旧名＞ 批量改名为 ＜新名＞，范围限 ＜目录＞。\n"
            "用 qoder_delegate 外包：scope 只列相关文件，dod 用 `pytest -q` 判定。"
        ),
    ),
    Skill(
        id="delegate",
        group="通用",
        label="外包给 Qoder",
        hint="要读很多文件或跑很久的活：主上下文只收结论",
        focus="＜任务＞",
        template=(
            "把这件事外包给 qoder_delegate：＜任务＞\n"
            "scope 只列允许改的文件，dod 写一条能判定成败的命令；\n"
            "status=partial 时先自己复核 checks，再决定报完成还是继续修。"
        ),
    ),
    Skill(
        id="write_docs",
        group="通用",
        label="写 README / 文档",
        hint="按现有代码生成，不编造接口",
        focus="＜README或模块名＞",
        template=(
            "根据当前代码写 ＜README或模块名＞，只写代码里真实存在的用法和参数，"
            "不要编造接口；写完给出一个最小可运行示例。"
        ),
    ),
)


def user_skill_dir(workspace: str | Path) -> Path:
    return Path(workspace) / USER_SKILL_DIR


def load_user_skills(workspace: str | Path | None) -> list[Skill]:
    """读 ``<workspace>/.gangge/skills/*.json``；坏文件跳过，不影响菜单。"""
    if not workspace:
        return []
    directory = user_skill_dir(workspace)
    if not directory.is_dir():
        return []
    skills: list[Skill] = []
    for path in sorted(directory.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                skills.extend(Skill.from_dict(item) for item in data)
            else:
                skills.append(Skill.from_dict(data))
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("[skills] 跳过 %s: %s", path.name, exc)
    return skills


def all_skills(workspace: str | Path | None = None) -> list[Skill]:
    """内置 + 用户自定义；同 id 时用户定义覆盖内置。"""
    merged: dict[str, Skill] = {s.id: s for s in BUILTIN_SKILLS}
    for skill in load_user_skills(workspace):
        merged[skill.id] = skill
    return list(merged.values())


def grouped(skills: list[Skill]) -> list[tuple[str, list[Skill]]]:
    """按分组聚合并保持首次出现顺序（菜单顺序 = 这里的顺序）。"""
    order: list[str] = []
    buckets: dict[str, list[Skill]] = {}
    for skill in skills:
        if skill.group not in buckets:
            buckets[skill.group] = []
            order.append(skill.group)
        buckets[skill.group].append(skill)
    return [(group, buckets[group]) for group in order]


def find(skill_id: str, workspace: str | Path | None = None) -> Skill | None:
    return next((s for s in all_skills(workspace) if s.id == skill_id), None)


def preview_image(skill: Skill) -> Path | None:
    """``skill`` 的效果预览图；没有/找不到/越界都返回 ``None``。

    用户自定义 skill 的 ``preview`` 也走这里，所以要挡住 ``..`` 这类路径，
    免得一条 json 就能让界面去读任意文件。
    """
    if not skill.preview:
        return None
    path = (PREVIEW_DIR / skill.preview).resolve()
    if not path.is_relative_to(PREVIEW_DIR.resolve()):
        logger.warning("[skills] 预览图越界，忽略: %s", skill.preview)
        return None
    return path if path.is_file() else None
