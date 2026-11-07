"""页面模板：HTML 骨架是**文件**，不是 Python 里的字符串。

原来的写法把三套上百行的 HTML/CSS/JS 塞在 `.py` 的三引号里：改一行版式要翻代码、
做 diff 看不出改在哪、想在别的项目里复用还得整段复制。现在它们各占一个文件：

* ``pages/storyboard.html`` —— 朴素分镜页（逐镜头时间轴驱动）；
* ``pages/showcase.html``   —— 3D 展示页（浏览器框 + 滚动即机位）；
* ``pages/site_inner.html`` —— 网站截图内页（``__seek(t)`` 平移长图）；
* ``pages/person.html``     —— 人物光线纪录片（俯视一张光路地图）；
* ``pages/flight.html``     —— 时间线飞行页（Three.js 三维光路：夜色空间 / 白天沙盘）。

``showcase`` 与 ``site`` 共用同一个外壳，区别只是框里装的东西；``flight`` 与
``miniature`` 同理，共用一份 Three.js 引擎，只换世界——所以它们指向同一个文件，
注册表里写明，:func:`gangge.layer4_tools.storyboard.pipeline.write_page` 按名字取。

新增一种页面：放一个 ``.html`` 进来、在 ``TEMPLATES`` 里登记一行，CLI 的 ``--page``、
``demo_video`` 的 ``page`` 参数和技能菜单就都能选到它——不用再改渲染代码。

模板用 :class:`string.Template` 的 ``$name`` 占位；CSS 里真要写美元符就写 ``$$``。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

#: 模板文件目录。随包分发（见 pyproject 的 ``package-data``），安装后就在包内。
PAGE_DIR = Path(__file__).resolve().parent / "pages"

#: 页面类型 → 模板文件。``site`` 复用展示页外壳，内页换成长截图。
#: ``studio`` 不是拍摄页面，而是样式工作台的外壳。
TEMPLATES: dict[str, str] = {
    "storyboard": "storyboard.html",
    "showcase": "showcase.html",
    "site": "showcase.html",
    "person": "person.html",
    "flight": "flight.html",
    "miniature": "flight.html",
    "site_inner": "site_inner.html",
    "studio": "studio.html",
}

#: 能拍成视频的页面（``site_inner`` 是内页片段，不是整页）。
PAGE_KINDS: tuple[str, ...] = (
    "storyboard",
    "showcase",
    "site",
    "person",
    "flight",
    "miniature",
)

#: 吃"节点"分镜的页面：一个镜头 = 时间线上的一个站点。
#: 写分镜的提示词、照片目录的分配规则都按这个集合判断，不各写一份。
NODE_PAGES: tuple[str, ...] = ("person", "flight", "miniature")


@lru_cache(maxsize=None)
def read_page(name: str) -> str:
    """按注册名读出模板全文；名字不认得就报出可选值。"""
    try:
        filename = TEMPLATES[name]
    except KeyError:
        raise KeyError(
            f"未知页面模板: {name!r}，可选: {', '.join(TEMPLATES)}"
        ) from None
    path = PAGE_DIR / filename
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise FileNotFoundError(
            f"页面模板缺失: {path}（安装包时检查 pyproject 的 package-data 是否包含 pages/*.html）"
        ) from e


def template_path(name: str) -> Path:
    """模板文件路径——给"想改版式"的人和测试用。"""
    return PAGE_DIR / TEMPLATES[name]


def page_names() -> tuple[str, ...]:
    """可以直接拍成视频的页面名（CLI / 工具的枚举都取自这里）。"""
    return PAGE_KINDS
