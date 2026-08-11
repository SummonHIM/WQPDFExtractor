import asyncio
import io
import re
import shutil
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import img2pdf
import pikepdf
from PIL import Image
from playwright.async_api import Page, Route


class BookExtractor:
    def __init__(self, page: Page, output_dir: Path, temp_dir: Path, page_key: str, force: bool):
        self.page = page
        self.output_dir = output_dir
        self.temp_dir = temp_dir
        self.page_key = page_key
        self.force = force
        self.bid = self._parse_bid(page.url)
        self.title = None
        self.image_dir = None
        self.temp_image_dir = None
        self.saved_pages = set()
        self._image_cache: dict[str, bytes] = {}

    @staticmethod
    def _parse_bid(url: str) -> str:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        return params.get("bid", ["unknown"])[0]

    async def _intercept_images(self, route: Route):
        try:
            response = await route.fetch()
            body = await response.body()
            url = route.request.url
            parsed = urlparse(url)
            path_key = parsed.path + "?" + parsed.query if parsed.query else parsed.path
            self._image_cache[path_key] = body
            await route.fulfill(response=response)
        except Exception:
            await route.continue_()

    async def extract(self):
        print(f"[{self.bid}] 开始提取...")

        await self.page.route("**/deep/page/lmg/**", self._intercept_images)

        await self.page.wait_for_selector("#pb", timeout=30000)
        self.title = await self._get_title()
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", self.title)

        # output/bid/images 和 output/bid/书名.pdf
        book_dir = self.output_dir / self.bid
        self.image_dir = book_dir / "images"
        self.image_dir.mkdir(parents=True, exist_ok=True)

        # 临时目录/WQPDFExtractor/page_key
        self.temp_image_dir = self.temp_dir / self.page_key
        self.temp_image_dir.mkdir(parents=True, exist_ok=True)

        print(f"[{self.bid}] 书名: {self.title}")
        print(f"[{self.bid}] 输出目录: {book_dir}")
        print(f"[{self.bid}] 临时目录: {self.temp_image_dir}")

        page_indices = await self._get_page_indices()
        total_pages = len(page_indices)
        print(f"[{self.bid}] 共 {total_pages} 页")

        # 检查已有的页面，跳过已下载的（除非 --force）
        if self.force:
            print(f"[{self.bid}] --force 模式，清空已有图片")
            for f in self.image_dir.glob("*.png"):
                f.unlink()
        else:
            existing = set()
            for f in self.image_dir.glob("*.png"):
                try:
                    idx = int(f.stem)
                    if f.stat().st_size > 0:
                        existing.add(idx)
                except ValueError:
                    pass
            if existing:
                self.saved_pages = existing
                print(f"[{self.bid}] 已有 {len(existing)}/{total_pages} 页，跳过已下载的")

        missing = [i for i in page_indices if i not in self.saved_pages]
        if not missing:
            print(f"[{self.bid}] 所有页面已下载，直接合成PDF")
        else:
            print(f"[{self.bid}] 需要下载 {len(missing)} 页")
            for i in missing:
                try:
                    await asyncio.wait_for(self._extract_page(i, total_pages), timeout=60)
                except asyncio.TimeoutError:
                    print(f"[{self.bid}] 页 {i} 超时60s，刷新页面重试...")
                    await self._reload_and_wait()
                    try:
                        await asyncio.wait_for(self._extract_page(i, total_pages), timeout=60)
                    except asyncio.TimeoutError:
                        print(f"[{self.bid}] 页 {i} 重试后仍超时，跳过")
                    except Exception as e:
                        print(f"[{self.bid}] 页 {i} 重试异常: {e}")
                except Exception as e:
                    print(f"[{self.bid}] 页 {i} 异常: {e}")
                    traceback.print_exc()

        await self.page.unroute("**/deep/page/lmg/**", self._intercept_images)

        # 提取目录
        toc = await self._extract_toc()
        if toc:
            print(f"[{self.bid}] 提取到 {len(toc)} 个目录项")

        pdf_path = book_dir / f"{safe_title}.pdf"
        self._compile_pdf(pdf_path, page_indices, toc)
        print(f"[{self.bid}] PDF 已保存: {pdf_path}")

        # 清理临时目录
        shutil.rmtree(self.temp_image_dir, ignore_errors=True)

    async def _get_title(self) -> str:
        try:
            el = await self.page.wait_for_selector(
                ".read-header-name", timeout=10000
            )
            return (await el.inner_text()).strip()
        except Exception:
            return f"book_{self.bid}"

    async def _get_page_indices(self) -> list[int]:
        indices = await self.page.evaluate("""() => {
            const boxes = document.querySelectorAll('#pb .page-img-box');
            const result = [];
            for (const box of boxes) {
                const idx = box.getAttribute('index');
                if (idx && idx !== '') {
                    const n = parseInt(idx);
                    if (!isNaN(n)) result.push(n);
                }
            }
            return result.sort((a, b) => a - b);
        }""")
        return indices or []

    async def _extract_page(self, index: int, total_pages: int):
        if index in self.saved_pages:
            return

        selector = f'#pb .page-img-box[index="{index}"]'
        box = await self.page.query_selector(selector)
        if not box:
            print(f"[{self.bid}] 页 {index} 未找到")
            return

        await self.page.evaluate("""(sel) => {
            const el = document.querySelector(sel);
            if (el) el.scrollIntoView({ block: 'center' });
        }""", selector)
        await asyncio.sleep(1)

        loaded = await self._wait_for_slices_loaded(selector, index)
        if not loaded:
            print(f"[{self.bid}] 页 {index} 切片未加载完成")
            return

        layout = await self._get_page_layout(selector)
        if not layout:
            print(f"[{self.bid}] 页 {index} 获取布局失败")
            return

        # 先保存到临时目录
        temp_path = self.temp_image_dir / f"{index:04d}.png"
        self._compose_page(layout, temp_path)

        if not temp_path.exists() or temp_path.stat().st_size == 0:
            print(f"[{self.bid}] 页 {index} 保存失败")
            return

        # 复制到正式目录
        final_path = self.image_dir / f"{index:04d}.png"
        shutil.copy2(str(temp_path), str(final_path))

        self.saved_pages.add(index)
        print(f"[{self.bid}] 页 {index}/{total_pages} 已保存 ({final_path.stat().st_size} bytes)")

    async def _wait_for_slices_loaded(self, box_selector: str, index: int) -> bool:
        img_l_selector = f'{box_selector} .page_img_l'

        for attempt in range(120):
            container = await self.page.query_selector(img_l_selector)
            if not container:
                if attempt % 20 == 0:
                    print(f"[{self.bid}] 页 {index} 等待 .page_img_l 出现... ({attempt})")
                await asyncio.sleep(0.5)
                continue

            imgs = await container.query_selector_all("img")
            if not imgs:
                if attempt % 20 == 0:
                    print(f"[{self.bid}] 页 {index} .page_img_l 内无 img... ({attempt})")
                await asyncio.sleep(0.5)
                continue

            all_loaded = True
            for img in imgs:
                src = await img.get_attribute("src")
                if not src or src == "":
                    all_loaded = False
                    break

            if all_loaded:
                await asyncio.sleep(0.5)
                print(f"[{self.bid}] 页 {index} 切片已加载 ({len(imgs)} 片)")
                return True

            await asyncio.sleep(0.5)

        return False

    async def _get_page_layout(self, box_selector: str) -> dict | None:
        return await self.page.evaluate("""(boxSelector) => {
            const box = document.querySelector(boxSelector);
            if (!box) return null;

            const pageSl = box.querySelector('.page_sl');
            if (!pageSl) return null;

            const style = pageSl.getAttribute('style') || '';
            const wMatch = style.match(/width:\\s*(\\d+)px/);
            const hMatch = style.match(/height:\\s*(\\d+)px/);
            const canvasWidth = wMatch ? parseInt(wMatch[1]) : 1439;
            const canvasHeight = hMatch ? parseInt(hMatch[1]) : 2005;

            let rotation = 0;
            const plg = box.querySelector('.plg');
            if (plg) {
                const plgStyle = plg.getAttribute('style') || '';
                const matrixMatch = plgStyle.match(/matrix\\(([^)]+)\\)/);
                if (matrixMatch) {
                    const vals = matrixMatch[1].split(',').map(v => parseFloat(v.trim()));
                    const a = vals[0], b = vals[1];
                    let angle = Math.round(Math.atan2(b, a) * 180 / Math.PI);
                    if (angle < 0) angle += 360;
                    rotation = angle;
                }
                const rotateMatch = plgStyle.match(/rotate\\(([\\d.]+)deg\\)/);
                if (rotateMatch) {
                    rotation = Math.round(parseFloat(rotateMatch[1])) % 360;
                }
            }

            const imgs = box.querySelectorAll('.page_img_l img');
            const slices = [];
            for (const img of imgs) {
                const imgStyle = img.getAttribute('style') || '';
                const leftMatch = imgStyle.match(/left:\\s*(\\d+)px/);
                const widthMatch = imgStyle.match(/width:\\s*(\\d+)px/);
                slices.push({
                    src: img.getAttribute('src') || '',
                    left: leftMatch ? parseInt(leftMatch[1]) : 0,
                    width: widthMatch ? parseInt(widthMatch[1]) : img.naturalWidth,
                });
            }

            return { canvasWidth, canvasHeight, rotation, slices };
        }""", box_selector)

    def _compose_page(self, layout: dict, img_path: Path):
        canvas_width = layout["canvasWidth"]
        canvas_height = layout["canvasHeight"]
        rotation = layout["rotation"]
        slices = layout["slices"]

        canvas = Image.new("RGB", (canvas_width, canvas_height), (255, 255, 255))

        for i, s in enumerate(slices):
            src = s["src"]
            left = s["left"]
            width = s["width"]

            img_bytes = self._find_cached_image(src)
            if not img_bytes:
                print(f"[{self.bid}]   切片 {i} 未在缓存中找到 (src: {src[:50]})")
                continue

            try:
                piece = Image.open(io.BytesIO(img_bytes))
                if width > 0 and canvas_height > 0:
                    piece = piece.resize((width, canvas_height), Image.LANCZOS)
                canvas.paste(piece, (left, 0))
            except Exception as e:
                print(f"[{self.bid}]   切片 {i} 拼接失败: {e}")

        if rotation == 180:
            canvas = canvas.rotate(180, expand=False)
        elif rotation == 90:
            canvas = canvas.rotate(-90, expand=True)
        elif rotation == 270:
            canvas = canvas.rotate(-270, expand=True)

        canvas.save(str(img_path), "PNG")

    def _find_cached_image(self, src: str) -> bytes | None:
        if not src:
            return None

        if src in self._image_cache:
            return self._image_cache[src]

        parsed = urlparse(src)
        path_key = parsed.path + "?" + parsed.query if parsed.query else parsed.path
        if path_key in self._image_cache:
            return self._image_cache[path_key]

        for key, data in self._image_cache.items():
            if parsed.path in key:
                return data

        return None

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

            toc = await self.page.evaluate("""() => {
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
                        if (children) {
                            entry.children = parseNodes(children, depth + 1);
                        }
                        result.push(entry);
                    }
                    return result;
                }
                const tree = document.querySelector('.book-tree');
                if (!tree) return [];
                return parseNodes(tree, 0);
            }""")
            return toc or []
        except Exception as e:
            print(f"[{self.bid}] 提取目录失败: {e}")
            return []

    async def _reload_and_wait(self):
        await self.page.reload(wait_until="domcontentloaded")
        await self.page.wait_for_selector("#pb", timeout=30000)
        print(f"[{self.bid}] 页面已刷新")

    def _compile_pdf(self, pdf_path: Path, page_indices: list[int], toc: list[dict]):
        image_files = []
        for i in page_indices:
            img_path = self.image_dir / f"{i:04d}.png"
            if img_path.exists():
                image_files.append(str(img_path))

        if not image_files:
            print(f"[{self.bid}] 没有图片可合成PDF")
            return

        print(f"[{self.bid}] 合成PDF: {len(image_files)} 页")
        with open(pdf_path, "wb") as f:
            f.write(img2pdf.convert(image_files))

        if not toc:
            return

        try:
            page_to_idx = {page: idx for idx, page in enumerate(page_indices)}

            with pikepdf.open(pdf_path, allow_overwriting_input=True) as pdf:
                with pdf.open_outline() as outline:
                    self._add_bookmarks(outline.root, toc, page_to_idx, pdf)
                pdf.save(pdf_path)
            print(f"[{self.bid}] 书签已添加")
        except Exception as e:
            print(f"[{self.bid}] 添加书签失败: {e}")
            traceback.print_exc()

    def _add_bookmarks(self, parent, items: list[dict], page_map: dict, pdf):
        for item in items:
            page_num = item["page"]
            title = item["title"]

            if page_num in page_map:
                page_idx = page_map[page_num]
            else:
                closest = min(page_map.keys(), key=lambda p: abs(p - page_num), default=None)
                if closest is None:
                    continue
                page_idx = page_map[closest]

            bookmark = pikepdf.OutlineItem(title, page_idx)
            parent.append(bookmark)

            children = item.get("children", [])
            if children:
                self._add_bookmarks(bookmark.children, children, page_map, pdf)
