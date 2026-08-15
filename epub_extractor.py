import asyncio
import base64
import re
import shutil
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import pikepdf
from playwright.async_api import Page, async_playwright

TRIAL_END_TEXT = "您的试读已结束"
CSS_URL_RE = re.compile(r'url\(\s*["\']?\s*([^"\')\s]+)\s*["\']?\s*\)')


class EpubExtractor:
    def __init__(self, page: Page, output_dir: Path, temp_dir: Path, page_key: str, force: bool):
        self.page = page
        self.output_dir = output_dir
        self.temp_dir = temp_dir
        self.page_key = page_key
        self.force = force
        self.bid = self._parse_bid(page.url)
        self.title = None
        self._resource_cache: dict[str, bytes] = {}

    @staticmethod
    def _parse_bid(url: str) -> str:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        return params.get("bid", ["unknown"])[0]

    async def _get_title(self) -> str:
        try:
            el = await self.page.wait_for_selector(".read-header-name", timeout=10000)
            return (await el.inner_text()).strip()
        except Exception:
            return f"book_{self.bid}"

    async def _get_page_info(self) -> tuple[int, int]:
        text = await self.page.evaluate("""() => {
            const el = document.querySelector('.page-head-tol');
            return el ? el.textContent.trim() : '';
        }""")
        m = re.match(r"(\d+)\s*/\s*(\d+)", text)
        if m:
            return int(m.group(1)), int(m.group(2))
        return 1, 1

    async def _check_trial_end(self) -> bool:
        text = await self.page.evaluate("""() => document.body.innerText""")
        if TRIAL_END_TEXT in text:
            return True
        frame = await self._get_epub_frame()
        if frame:
            try:
                frame_text = await frame.evaluate("""() => document.body.innerText""")
                if TRIAL_END_TEXT in frame_text:
                    return True
            except Exception:
                pass
        return False

    async def _get_epub_frame(self):
        handle = await self.page.query_selector("#iFrame")
        if handle:
            return await handle.content_frame()
        return None

    async def _switch_to_single_column(self):
        try:
            result = await self.page.evaluate("""() => {
                const popup = document.querySelector('.style-popup');
                if (popup) popup.style.display = 'block';
                const btns = document.querySelectorAll('.infeed-wrapper .popup-content-row a');
                for (const btn of btns) {
                    if (btn.textContent.trim().includes('单栏')) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }""")
            if result:
                print(f"[{self.bid}] [EPUB] 已切换到单栏模式")
            await asyncio.sleep(1)
        except Exception:
            pass

    async def _click_first_chapter(self):
        await self.page.evaluate("""() => {
            const nodes = document.querySelectorAll('.book-tree .el-tree-node .tree-node.canread');
            if (nodes.length > 0) nodes[0].click();
        }""")
        await asyncio.sleep(1)

    async def _click_next_chapter(self) -> bool:
        return await self.page.evaluate("""() => {
            const btns = document.querySelectorAll('.read-content-btn-wrapper a');
            for (const btn of btns) {
                if (btn.textContent.trim().includes('下一章节')) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }""")

    async def _wait_for_chapter_load(self):
        for _ in range(60):
            frame = await self._get_epub_frame()
            if frame:
                try:
                    ready = await frame.evaluate("""() => {
                        return document.readyState === 'complete'
                            && document.body
                            && document.body.innerHTML.length > 100;
                    }""")
                    if ready:
                        await asyncio.sleep(0.5)
                        return True
                except Exception:
                    pass
            await asyncio.sleep(0.5)
        return False

    async def _get_chapter_identifier(self) -> str:
        frame = await self._get_epub_frame()
        if not frame:
            return ""
        try:
            return urlparse(frame.url).path
        except Exception:
            return ""

    async def _fetch_resource(self, frame, url: str) -> bytes | None:
        if url in self._resource_cache:
            return self._resource_cache[url]
        try:
            data_url = await frame.evaluate("""async (url) => {
                try {
                    const resp = await fetch(url, { credentials: 'include' });
                    if (!resp.ok) return null;
                    const blob = await resp.blob();
                    return await new Promise(resolve => {
                        const reader = new FileReader();
                        reader.onload = () => resolve(reader.result);
                        reader.readAsDataURL(blob);
                    });
                } catch(e) { return null; }
            }""", url)
            if not data_url or "," not in data_url:
                return None
            _, b64 = data_url.split(",", 1)
            data = base64.b64decode(b64)
            self._resource_cache[url] = data
            return data
        except Exception:
            return None

    def _unique_filename(self, url: str, seen: set) -> str:
        name = Path(urlparse(url).path).name or "resource"
        if name not in seen:
            seen.add(name)
            return name
        stem, suffix = Path(name).stem, Path(name).suffix
        i = 1
        while f"{stem}_{i}{suffix}" in seen:
            i += 1
        result = f"{stem}_{i}{suffix}"
        seen.add(result)
        return result

    async def _process_css_urls(self, frame, css_text: str, css_url: str, assets_dir: Path, seen: set) -> str:
        for ref in CSS_URL_RE.findall(css_text):
            if ref.startswith("data:") or ref.startswith("#"):
                continue
            full = urljoin(css_url, ref)
            data = await self._fetch_resource(frame, full)
            if not data:
                continue
            filename = self._unique_filename(full, seen)
            (assets_dir / filename).write_bytes(data)
            css_text = css_text.replace(ref, filename)
        return css_text

    async def _scroll_to_load_all(self, frame):
        img_count = await frame.evaluate("() => document.querySelectorAll('img').length")
        if img_count == 0:
            return
        print(f"[{self.bid}] [EPUB] 滚动加载 {img_count} 张图片...")
        for i in range(img_count):
            await frame.evaluate("""(idx) => {
                const imgs = document.querySelectorAll('img');
                if (imgs[idx]) imgs[idx].scrollIntoView({ block: 'center' });
            }""", i)
            await asyncio.sleep(0.3)

    async def _save_chapter(self, chapter_dir: Path) -> bool:
        frame = await self._get_epub_frame()
        if not frame:
            return False

        await self._scroll_to_load_all(frame)

        assets_dir = chapter_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)

        resources = await frame.evaluate("""() => {
            const res = [];
            document.querySelectorAll('link[rel="stylesheet"]').forEach(l => {
                if (l.href) res.push({ fullUrl: l.href, attr: l.getAttribute('href'), type: 'css' });
            });
            document.querySelectorAll('img').forEach(img => {
                if (img.src && !img.src.startsWith('data:'))
                    res.push({ fullUrl: img.src, attr: img.getAttribute('src'), type: 'img' });
            });
            // SVG <image> 的 xlink:href 和 href
            document.querySelectorAll('image').forEach(img => {
                const href = img.getAttribute('xlink:href') || img.getAttribute('href');
                if (href && !href.startsWith('data:')) {
                    try {
                        const absolute = new URL(href, document.baseURI).href;
                        res.push({ fullUrl: absolute, attr: href, type: 'img' });
                    } catch(e) {}
                }
            });
            // background-image 等通过 style 引用的图片
            document.querySelectorAll('[style]').forEach(el => {
                const style = el.getAttribute('style') || '';
                const matches = style.match(/url\\(["']?([^"')]+)["']?\\)/g);
                if (matches) {
                    for (const m of matches) {
                        const urlMatch = m.match(/url\\(["']?([^"')]+)["']?\\)/);
                        if (urlMatch && urlMatch[1] && !urlMatch[1].startsWith('data:')) {
                            // 解析为绝对 URL
                            try {
                                const absolute = new URL(urlMatch[1], document.baseURI).href;
                                res.push({ fullUrl: absolute, attr: urlMatch[1], type: 'other' });
                            } catch(e) {}
                        }
                    }
                }
            });
            return res;
        }""")

        attr_to_local = {}
        seen_names = set()

        for res in resources:
            full_url = res["fullUrl"]
            attr = res["attr"]

            data = await self._fetch_resource(frame, full_url)
            if not data:
                continue

            filename = self._unique_filename(full_url, seen_names)
            (assets_dir / filename).write_bytes(data)
            attr_to_local[attr] = f"assets/{filename}"

            if res["type"] == "css":
                css_text = data.decode("utf-8", errors="ignore")
                updated = await self._process_css_urls(frame, css_text, full_url, assets_dir, seen_names)
                if updated != css_text:
                    (assets_dir / filename).write_text(updated, encoding="utf-8")

        html = await frame.evaluate("""() => {
            const clone = document.documentElement.cloneNode(true);
            clone.querySelectorAll('script').forEach(el => el.remove());
            clone.querySelectorAll('#tooltip, .a_tooltip, .s_tip').forEach(el => el.remove());
            clone.querySelectorAll('.custom-horizontal-scrollbar').forEach(el => el.remove());
            return '<!DOCTYPE html>' + clone.outerHTML;
        }""")

        for attr, local in attr_to_local.items():
            html = html.replace(attr, local)

        (chapter_dir / "index.html").write_text(html, encoding="utf-8")
        return True

    async def extract(self):
        print(f"[{self.bid}] [EPUB] 开始提取...")
        await self.page.wait_for_selector("#pagebox", timeout=30000)
        self.title = await self._get_title()
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", self.title)

        book_dir = self.output_dir / self.bid
        html_dir = book_dir / "html"
        html_dir.mkdir(parents=True, exist_ok=True)

        temp_dir = self.temp_dir / self.page_key
        temp_dir.mkdir(parents=True, exist_ok=True)

        print(f"[{self.bid}] [EPUB] 书名: {self.title}")
        print(f"[{self.bid}] [EPUB] 输出目录: {book_dir}")

        _, total_pages = await self._get_page_info()
        print(f"[{self.bid}] [EPUB] 共 {total_pages} 页")

        pdf_path = book_dir / f"{safe_title}.pdf"

        if self.force:
            print(f"[{self.bid}] [EPUB] --force 模式，清空已有章节")
            for d in html_dir.iterdir():
                if d.is_dir():
                    shutil.rmtree(d)

        await self._switch_to_single_column()

        # === 第一步：逐章保存 HTML + 资源 ===
        existing = sorted([d for d in html_dir.iterdir() if d.is_dir() and (d / "index.html").exists()])
        if existing and not self.force:
            print(f"[{self.bid}] [EPUB] 已有 {len(existing)} 个章节，跳过提取")
        else:
            print(f"[{self.bid}] [EPUB] 跳转到第1章...")
            await self._click_first_chapter()
            await self._wait_for_chapter_load()

            seen_paths = set()
            chapter_num = 0

            while True:
                if await self._check_trial_end():
                    print(f"[{self.bid}] [EPUB] 试读已结束，停止提取")
                    break

                await self._wait_for_chapter_load()

                chapter_path = await self._get_chapter_identifier()
                if chapter_path in seen_paths:
                    break
                seen_paths.add(chapter_path)

                chapter_num += 1
                current_page, total = await self._get_page_info()

                chapter_dir = html_dir / f"{chapter_num:04d}"
                chapter_dir.mkdir(parents=True, exist_ok=True)

                ok = await self._save_chapter(chapter_dir)
                if ok:
                    print(f"[{self.bid}] [EPUB] 章节 {chapter_num} (页 {current_page}/{total}) 已保存")
                else:
                    print(f"[{self.bid}] [EPUB] 章节 {chapter_num} 保存失败")

                if current_page >= total:
                    break

                clicked = await self._click_next_chapter()
                if not clicked:
                    print(f"[{self.bid}] [EPUB] 没有下一章节按钮，停止")
                    break
                await asyncio.sleep(1)

        # === 第二步：HTML 转 PDF 并合并 ===
        chapter_dirs = sorted([d for d in html_dir.iterdir() if d.is_dir() and (d / "index.html").exists()])
        if not chapter_dirs:
            print(f"[{self.bid}] [EPUB] 没有章节可转换")
            return

        print(f"[{self.bid}] [EPUB] 开始转换 {len(chapter_dirs)} 个章节为PDF...")
        chapter_pdfs = await self._convert_chapters_to_pdf(chapter_dirs, temp_dir)

        # toc = await self._extract_toc()
        # if toc:
        #     print(f"[{self.bid}] [EPUB] 提取到目录")

        self._merge_pdfs(pdf_path, chapter_pdfs, [])
        print(f"[{self.bid}] [EPUB] PDF 已保存: {pdf_path}")

        shutil.rmtree(temp_dir, ignore_errors=True)

    async def _convert_chapters_to_pdf(self, chapter_dirs: list[Path], temp_dir: Path) -> list[Path]:
        pdf_paths = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(viewport={"width": 922, "height": 1304})
            page = await context.new_page()

            for ch_dir in chapter_dirs:
                html_file = ch_dir / "index.html"
                try:
                    file_url = html_file.as_uri()
                    await page.goto(file_url, wait_until="networkidle")

                    await page.evaluate("""() => {
                        const style = document.createElement('style');
                        style.textContent = `
                            @media print {
                                body, html {
                                    display: block !important;
                                    visibility: visible !important;
                                }
                            }
                            *, *::before, *::after {
                                columns: auto !important;
                                column-count: auto !important;
                                column-width: auto !important;
                                column-gap: normal !important;
                                column-fill: auto !important;
                                overflow: visible !important;
                                max-height: none !important;
                                height: auto !important;
                            }
                            html, body {
                                width: 100% !important;
                                height: auto !important;
                                overflow: visible !important;
                                position: static !important;
                            }
                        `;
                        document.head.appendChild(style);
                        // 直接设置 style 属性，优先级最高
                        document.body.style.setProperty('background', 'white', 'important');
                        document.body.style.setProperty('background-color', 'white', 'important');
                        document.documentElement.style.setProperty('background', 'white', 'important');
                        document.documentElement.style.setProperty('background-color', 'white', 'important');
                        // 移除 themes class
                        document.body.className = document.body.className.replace(/themes\d/g, '');
                    }""")
                    await asyncio.sleep(0.5)

                    pdf_bytes = await page.pdf(
                        format="A4",
                        print_background=True,
                        margin={"top": "0.5in", "bottom": "0.5in", "left": "0.5in", "right": "0.5in"},
                    )

                    out = temp_dir / f"{ch_dir.name}.pdf"
                    out.write_bytes(pdf_bytes)
                    pdf_paths.append(out)
                    print(f"[{self.bid}] [EPUB] {ch_dir.name} -> PDF ({len(pdf_bytes)} bytes)")
                except Exception as e:
                    print(f"[{self.bid}] [EPUB] {ch_dir.name} 转PDF失败: {e}")
                    traceback.print_exc()

            await browser.close()
        return pdf_paths

    async def _extract_toc(self) -> list[dict]:
        try:
            for _ in range(20):
                collapsed = await self.page.evaluate("""() => {
                    const icons = document.querySelectorAll('.book-tree .el-tree-node__expand-icon:not(.expanded):not(.is-leaf)');
                    for (const icon of icons) icon.click();
                    return icons.length;
                }""")
                if collapsed == 0:
                    break
                await asyncio.sleep(0.3)

            return await self.page.evaluate("""() => {
                function parseNodes(container, depth) {
                    const result = [];
                    const nodes = container.querySelectorAll(':scope > .el-tree-node');
                    for (const node of nodes) {
                        const content = node.querySelector(':scope > .el-tree-node__content');
                        if (!content) continue;
                        const titleEl = content.querySelector('.node-left');
                        const pageEl = content.querySelector('.node-right > span:first-child');
                        if (!titleEl || !pageEl) continue;
                        const title = titleEl.textContent.trim();
                        const page = parseInt(pageEl.textContent.trim());
                        if (!title || isNaN(page)) continue;
                        const entry = { title, page, depth };
                        const children = node.querySelector(':scope > .el-tree-node__children');
                        if (children) entry.children = parseNodes(children, depth + 1);
                        result.push(entry);
                    }
                    return result;
                }
                const tree = document.querySelector('.book-tree');
                return tree ? parseNodes(tree, 0) : [];
            }""") or []
        except Exception as e:
            print(f"[{self.bid}] [EPUB] 提取目录失败: {e}")
            return []

    def _merge_pdfs(self, output_path: Path, chapter_pdfs: list[Path], toc: list[dict]):
        if not chapter_pdfs:
            print(f"[{self.bid}] [EPUB] 没有章节PDF可合并")
            return

        merged = pikepdf.new()
        ch_offsets = []
        offset = 0

        for pdf_path in chapter_pdfs:
            try:
                src = pikepdf.open(pdf_path)
                ch_offsets.append(offset)
                merged.pages.extend(src.pages)
                offset += len(src.pages)
            except Exception as e:
                print(f"[{self.bid}] [EPUB] 合并 {pdf_path.name} 失败: {e}")
                ch_offsets.append(offset)

        print(f"[{self.bid}] [EPUB] 合并完成: {len(merged.pages)} 页")

        if toc and ch_offsets:
            try:
                with merged.open_outline() as outline:
                    self._add_bookmarks(outline.root, toc, ch_offsets)
                print(f"[{self.bid}] [EPUB] 书签已添加")
            except Exception as e:
                print(f"[{self.bid}] [EPUB] 添加书签失败: {e}")
                traceback.print_exc()

        merged.save(output_path)

    def _add_bookmarks(self, parent, items: list[dict], ch_offsets: list[int]):
        total_chapters = len(ch_offsets)
        for item in items:
            page_num = item["page"]
            if page_num >= 1 and page_num <= total_chapters:
                page_idx = ch_offsets[page_num - 1]
            elif page_num > total_chapters:
                page_idx = ch_offsets[-1] if ch_offsets else 0
            else:
                page_idx = 0
            bookmark = pikepdf.OutlineItem(item["title"], page_idx)
            parent.append(bookmark)
            children = item.get("children", [])
            if children:
                self._add_bookmarks(bookmark.children, children, ch_offsets)
