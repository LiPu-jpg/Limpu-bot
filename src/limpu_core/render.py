"""Markdown → 图片渲染器。

将课程详情 markdown 渲染为适合 QQ 群内查看的图片，用于合并转发。
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path

from nonebot.adapters.onebot.v11 import MessageSegment

CSS = """
body {
    font-family: "Noto Sans CJK SC", "Microsoft YaHei", "PingFang SC", sans-serif;
    font-size: 15px;
    line-height: 1.7;
    color: #333;
    max-width: 500px;
    margin: 0;
    padding: 20px 16px 30px;
    background: #fff;
}
h1 { font-size: 20px; margin: 0 0 12px; border-bottom: 2px solid #1a73e8; padding-bottom: 8px; }
h2 { font-size: 17px; margin: 18px 0 8px; color: #1a73e8; }
h3 { font-size: 15px; margin: 14px 0 6px; }
p { margin: 6px 0; }
ul, ol { margin: 6px 0 6px 0; padding-left: 22px; }
li { margin: 3px 0; }
code { background: #f0f0f0; padding: 1px 5px; border-radius: 3px; font-size: 13px; }
pre { background: #f5f5f5; padding: 10px; border-radius: 6px; overflow-x: auto; font-size: 13px; }
pre code { background: none; padding: 0; }
blockquote { border-left: 3px solid #1a73e8; padding-left: 12px; margin: 8px 0; color: #555; }
strong { color: #222; }
a { color: #1a73e8; }
table { border-collapse: collapse; width: 100%; margin: 8px 0; }
th, td { border: 1px solid #ddd; padding: 6px 10px; font-size: 13px; text-align: left; }
th { background: #f0f5ff; }
img { max-width: 100%; }
"""


async def md_to_image(markdown_text: str) -> str:
    """将 markdown 渲染为 PNG 图片，返回 base64 data URL。"""
    import markdown_it
    from playwright.async_api import async_playwright

    md = markdown_it.MarkdownIt()
    md.options["html"] = True
    md.options["breaks"] = True
    html_body = md.render(markdown_text)
    html = f"<!DOCTYPE html><html><head><meta charset=utf-8><style>{CSS}</style></head><body>{html_body}</body></html>"

    CHROMIUM = "/root/.cache/ms-playwright/chromium-1208/chrome-linux64/chrome"
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
            executable_path=CHROMIUM,
        )
        page = await browser.new_page(viewport={"width": 520, "height": 100})
        await page.set_content(html)
        # 获取内容真实高度
        height = await page.evaluate("() => document.body.scrollHeight")
        await page.set_viewport_size({"width": 520, "height": height + 40})
        img_bytes = await page.screenshot(full_page=True, type="png")
        await browser.close()

    b64 = base64.b64encode(img_bytes).decode()
    return f"base64://{b64}"


async def md_to_forward_node(markdown_text: str, name: str = "Limpu", uin: str = "3930262753") -> dict:
    """生成合并转发节点（含渲染后的图片）。"""
    img_url = await md_to_image(markdown_text)
    return {
        "type": "node",
        "data": {
            "name": name,
            "uin": uin,
            "content": MessageSegment.image(img_url),
        },
    }
