"""预览用的示例 storyboard：一条短片，够看出风格，也够当动画预览。

效果图（PNG）和效果动画（HTML）都从这一个样本出，所以「图片」和「动画」两个
页签里看到的是同一套东西，只是前者是定格、后者会动。
"""

from __future__ import annotations

from gangge.layer4_tools.storyboard.schema import Scene, Storyboard

#: 每套风格一句中文气质，图里当副标题，比 id 好认。
TAGS: dict[str, str] = {
    "dark": "克制深色 · 不挑内容",
    "neon": "发光科技 · 抓眼",
    "paper": "纸张书卷 · 慢下来",
    "glass": "玻璃拟态 · 高级感",
    "brutal": "撞色粗框 · 记忆点",
}


def demo_storyboard(style: str) -> Storyboard:
    """五帧样片：标题 → 要点 → 流程 → 金句 → 结尾。"""
    return Storyboard(
        title="三步做出演示动画",
        style=style,
        scenes=[
            Scene(
                template="title",
                title="三步做出演示动画",
                text=TAGS.get(style, style),
            ),
            Scene(
                template="bullets",
                title="三步",
                items=["定主题与口播", "挑一套视觉风格", "一键出片"],
            ),
            Scene(template="flow", title="流水线", nodes=["脚本", "配音", "渲染"]),
            Scene(
                template="quote",
                text="画面跟着声音走，改一帧只要一秒。",
                title="设计取舍",
            ),
            Scene(
                template="outro",
                title="试试看",
                text="选一条技能就能开始",
                items=["开始创作"],
            ),
        ],
    )


def title_only(style: str) -> Storyboard:
    """只要标题帧——出静态效果图时截第一帧就够了，省得渲染五帧。"""
    full = demo_storyboard(style)
    return Storyboard(
        title=full.title,
        style=style,
        scenes=full.scenes[:1],
    )
