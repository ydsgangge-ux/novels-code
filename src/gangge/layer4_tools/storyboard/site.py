"""网址 → 整页截图 → 3D 展示页：框里装的就是那个网站本身。

和 :mod:`gangge.layer4_tools.storyboard.showcase` 的区别只有"内页"这一块：

* showcase 的内页是**分镜动画**，滚一下换一个镜头；
* 这里的内页是**一张长截图**，滚一下整张图在框里往上走。

外壳（3D 舞台、机位推进、漂浮标签、鼠标视差、HUD）一行都没复制——把截图做成一个
只暴露 ``__seek(t)`` 的内页，再交给 ``render_showcase(inner_html=...)`` 装进去。
这也意味着它天然继承了那套接口：手动滚能看，逐帧录也能出片。

运行（需要 playwright，只在抓图时用）：

    python -m gangge.layer4_tools.storyboard.showcase -u https://example.com --shot
"""

from __future__ import annotations

import asyncio
import base64
import sys
from dataclasses import dataclass
from pathlib import Path
from string import Template

from gangge.layer4_tools.storyboard.pages import read_page
from gangge.layer4_tools.storyboard.schema import Scene, Storyboard
from gangge.layer4_tools.storyboard.showcase import CHAPTER_VH, TAIL_VH, render_showcase
from gangge.layer4_tools.storyboard.sources import _pretty_github_title, normalize_url

#: 抓图窗宽。1400 是"桌面站首屏"的常见断点，缩进框里也还看得清。
VIEW_WIDTH = 1400
VIEW_HEIGHT = 900
#: 最多截这么高。再长就是无限滚动的信息流，对展示没有意义，还撑爆文件。
MAX_HEIGHT = 5200
#: JPEG 质量：截图里全是文字，82 是"看不清"和"文件太大"之间的平衡点。
QUALITY = 82
#: 内页时间轴长度。只用来驱动滚动，不涉及配音，取个够平滑的值。
PAN_SECONDS = 10.0
#: 滚动长度（屏）。网页截图是一整条长图，滑太短会像"一下跳到底"。
SITE_SCREENS = 4.0
#: 这些风格本身是深色的，截图就跟着用站点自己的深色主题。
_DARK_STYLES = {"dark", "neon", "glass"}


class SiteError(RuntimeError):
    """截图没抓成（需要 playwright、网站打不开、页面是空的）。"""


@dataclass(frozen=True)
class SiteShot:
    """一张整页截图，以及它属于谁。"""

    url: str
    title: str
    description: str
    image: bytes
    width: int
    height: int

    def size_note(self) -> str:
        return f"{self.width}×{self.height}  {len(self.image) // 1024} KB"


def parse_host_map(text: str) -> dict[str, str]:
    """``github.com=140.82.112.4,example.com=1.2.3.4`` → ``{host: ip}``。

    DNS 被污染时（解析出来的是黑洞 IP，TCP 永远连不上），用 IP 直连绕过解析。
    只映射域名，不改 TLS 的 SNI，所以证书校验照常。
    """
    hosts: dict[str, str] = {}
    for item in (text or "").replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        host, sep, ip = item.partition("=")
        if not sep or not host.strip() or not ip.strip():
            raise ValueError(f"host 映射要写成 HOST=IP：{item!r}")
        hosts[host.strip()] = ip.strip()
    return hosts


async def capture_site(
    url: str,
    *,
    width: int = VIEW_WIDTH,
    height: int = VIEW_HEIGHT,
    max_height: int = MAX_HEIGHT,
    quality: int = QUALITY,
    color_scheme: str | None = "dark",
    timeout: float = 30.0,
    host_map: dict[str, str] | None = None,
) -> SiteShot:
    """用无头浏览器截一张整页图（JPEG）。

    ``color_scheme`` 默认 dark：展示框本身是深色的，里面塞一张纯白页面反差太大；
    站点支持 ``prefers-color-scheme`` 时跟着变暗，整页才像同一个作品。

    ``host_map`` 给域名钉死 IP（``{"github.com": "140.82.112.4"}``）：本机 DNS 被
    污染时，浏览器会先连到黑洞 IP 再超时——钉住一个能通的 IP 就正常了。
    """
    target = normalize_url(url)
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise SiteError(
            "需要 playwright 才能整页截图：pip install playwright && playwright install chromium"
        ) from e

    args = ["--hide-scrollbars"]
    if host_map:
        rules = ", ".join(f"MAP {host} {ip}" for host, ip in host_map.items())
        args.append(f"--host-resolver-rules={rules}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(args=args)
        try:
            page = await browser.new_page(
                viewport={"width": width, "height": height},
                device_scale_factor=1,
                color_scheme=color_scheme,
            )
            try:
                # 等 load 会被第三方字体/CDN 拖死（GitHub 页面就有几十个外链，
                # 少一个加载不出来，load 永远不触发）。先拿 DOM，再尽力等网络空闲。
                await page.goto(
                    target, wait_until="domcontentloaded", timeout=timeout * 1000
                )
            except Exception as e:  # noqa: BLE001 - 站点超时/拒连都算抓不到
                raise SiteError(f"打开页面失败：{e}") from e
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001 - 有资源卡住也照样截，别把整次抓图搭进去
                pass
            await page.evaluate("() => (document.fonts ? document.fonts.ready : null)")
            # 先滚一遍把懒加载的图都触发出来，再回到顶部截图，否则长图后段是空的
            await page.evaluate(
                """async (limit) => {
                     const step = window.innerHeight * 0.9;
                     const end = Math.min(document.body.scrollHeight, limit);
                     for (let y = 0; y < end; y += step) {
                       window.scrollTo(0, y);
                       await new Promise(r => setTimeout(r, 110));
                     }
                     window.scrollTo(0, 0);
                     await new Promise(r => setTimeout(r, 180));
                   }""",
                max_height,
            )
            meta = await page.evaluate(
                """() => ({
                     title: document.title || '',
                     desc: (document.querySelector('meta[name="description"]')
                            || document.querySelector('meta[property="og:description"]')
                            || {}).content || '',
                     h: Math.max(document.body.scrollHeight,
                                 document.documentElement.scrollHeight)
                   })"""
            )
            clip_h = max(height, min(int(meta.get("h") or height), max_height))
            image = await page.screenshot(
                # clip 必须配 full_page：否则超出视口的部分会被裁掉，只剩一屏
                full_page=True,
                clip={"x": 0, "y": 0, "width": width, "height": clip_h},
                type="jpeg",
                quality=quality,
                animations="disabled",
                caret="hide",
                timeout=int(timeout * 1000),
            )
        finally:
            await browser.close()

    if not image:
        raise SiteError(f"截图是空的：{target}")
    return SiteShot(
        url=target,
        title=str(meta.get("title") or "").strip(),
        description=" ".join(str(meta.get("desc") or "").split()),
        image=image,
        width=width,
        height=clip_h,
    )


def shot_inner_page(shot: SiteShot, t0: float = 0.0, t1: float = PAN_SECONDS) -> str:
    """把截图包成一个"只会上下平移"的内页。

    接口和分镜页一致（``__seek(t)`` / ``__hide_ui()``），所以外面的展示页不需要知道
    框里到底是动画还是图片。
    """
    encoded = base64.b64encode(shot.image).decode("ascii")
    return _INNER.substitute(
        alt=_html(shot.title or "site"),
        img=f"data:image/jpeg;base64,{encoded}",
        t0=f"{t0:.3f}",
        t1=f"{t1:.3f}",
    )


def build_site_page(
    url: str,
    out: Path | str | None = None,
    *,
    style: str = "neon",
    pan_screens: float = SITE_SCREENS,
    shot: SiteShot | None = None,
) -> Path:
    """抓图 + 生成展示页，返回 HTML 路径。``shot`` 传了就不抓（测试用）。"""
    shot = shot or asyncio.run(
        capture_site(url, color_scheme="dark" if style in _DARK_STYLES else "light")
    )
    target = Path(out) if out else Path(".gangge") / "showcase" / "site" / "index.html"
    if target.suffix.lower() not in (".html", ".htm"):
        target = target / "index.html"
    target.parent.mkdir(parents=True, exist_ok=True)

    head, note = _headline(shot.title, shot.description, shot.url)
    scene = Scene(
        template="title",
        title=head,
        text=note[:60],
        voiceover=note,          # HUD 的注解直接用它，别再显示"开场镜头"那种占位话
    )
    storyboard = Storyboard(title=head, style=style, scenes=[scene])
    # 16:10 比 16:9 多露一点内容，展示网页时更接近"笔记本屏幕"
    storyboard.width, storyboard.height = 1920, 1200
    slots = [(scene, 0.0, PAN_SECONDS)]
    html = render_showcase(
        storyboard,
        slots,
        source_url=shot.url,
        inner_html=shot_inner_page(shot, 0.3, PAN_SECONDS - 0.3),
        scroll_vh=max(1.0, pan_screens) * CHAPTER_VH + TAIL_VH,
    )
    target.write_text(html, encoding="utf-8")
    return target


def _headline(title: str, description: str, url: str) -> tuple[str, str]:
    """``owner/repo: 一句话介绍`` → 主标题取冒号前那截，注解取后面那截。

    站点标题常常是"品牌名 + 一句话"，整条塞进 HUD 会被截成半句英文，很难看。
    """
    title = _pretty_github_title(title) or url
    for sep in (":", "："):
        head, found, tail = title.partition(sep)
        if found and 2 <= len(head.strip()) <= 26 and tail.strip():
            return head.strip(), tail.strip()
    note = description.strip() or url
    if len(title) > 34:
        title = title[:33].rstrip() + "…"
    return title, note


def _html(text: str) -> str:
    import html as _mod

    return _mod.escape(str(text), quote=True)


_INNER = Template(read_page("site_inner"))


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    import argparse

    p = argparse.ArgumentParser(
        prog="python -m gangge.layer4_tools.storyboard.site",
        description="抓一个网站的整页截图，装进 3D 展示框（滚动时整页在框里向上走）。",
    )
    p.add_argument("url", help="要展示的网址")
    p.add_argument("-o", "--out", metavar="PATH", help="输出 HTML 或目录")
    p.add_argument("--style", default="neon", help="视觉风格（默认 neon）")
    p.add_argument("--pan-screens", type=float, default=4.0, help="滚动长度，单位是屏")
    args = p.parse_args()

    try:
        path = build_site_page(
            args.url, args.out, style=args.style, pan_screens=args.pan_screens
        )
    except SiteError as e:
        print(f"截图失败: {e}", file=sys.stderr)
        return 2
    print(f"展示页: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
