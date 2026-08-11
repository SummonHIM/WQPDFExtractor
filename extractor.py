import asyncio
import io
import re
import traceback
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import img2pdf
from PIL import Image
from playwright.async_api import Page, Route


class BookExtractor:
    def __init__(self, page: Page, output_dir: Path, page_key: str):
        self.page = page
        self.output_dir = output_dir
        self.page_key = page_key
        self.bid = self._parse_bid(page.url)
        self.title = None
        self.image_dir = None
        self.saved_pages = set()
        # 存储拦截到的图片数据: url -> bytes
        self._image_cache: dict[str, bytes] = {}

    @staticmethod
    def _parse_bid(url: str) -> str:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        return params.get("bid", ["unknown"])[0]

    async def _intercept_images(self, route: Route):
        """拦截图片请求，保存响应数据后放行"""
        try:
            response = await route.fetch()
            body = await response.body()
            url = route.request.url
            # 用路径部分作为 key（去掉域名，方便匹配 src 中的相对路径）
            parsed = urlparse(url)
            path_key = parsed.path + "?" + parsed.query if parsed.query else parsed.path
            self._image_cache[path_key] = body
            await route.fulfill(response=response)
        except Exception:
            await route.continue_()

    async def extract(self):
        print(f"[{self.bid}] 开始提取...")

        # 设置网络拦截，捕获所有切片图片
        await self.page.route("**/deep/page/lmg/**", self._intercept_images)

        await self.page.wait_for_selector("#pb", timeout=30000)
        self.title = await self._get_title()
        safe_title = re.sub(r'[\\/:*?"<>|]', "_", self.title)
        book_dir = self.output_dir / f"{self.bid}_{safe_title}" / self.page_key
        self.image_dir = book_dir / "images"
        self.image_dir.mkdir(parents=True, exist_ok=True)

        print(f"[{self.bid}] 书名: {self.title}")
        print(f"[{self.bid}] 输出目录: {book_dir}")

        page_indices = await self._get_page_indices()
        total_pages = len(page_indices)
        print(f"[{self.bid}] 共 {total_pages} 页")

        for i in page_indices:
            if i in self.saved_pages:
                continue
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

        # 移除拦截
        await self.page.unroute("**/deep/page/lmg/**", self._intercept_images)

        pdf_path = book_dir / f"{safe_title}.pdf"
        self._compile_pdf(pdf_path, page_indices)
        print(f"[{self.bid}] PDF 已保存: {pdf_path}")

    async def _get_title(self) -> str:
        try:
            el = await self.page.wait_for_selector(
                ".read-header-name", timeout=10000
            )
            return (await el.inner_text()).strip()
        except Exception:
            return f"book_{self.bid}"

    async def _get_page_indices(self) -> list[int]:
        """获取所有 page-img-box 的实际 index 值"""
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

        await box.scroll_into_view_if_needed()
        await asyncio.sleep(1)

        # 等待切片加载
        loaded = await self._wait_for_slices_loaded(selector, index)
        if not loaded:
            print(f"[{self.bid}] 页 {index} 切片未加载完成")
            return

        # 获取切片布局信息
        layout = await self._get_page_layout(selector)
        if not layout:
            print(f"[{self.bid}] 页 {index} 获取布局失败")
            return

        img_path = self.image_dir / f"{index:04d}.png"
        self._compose_page(layout, img_path)

        if img_path.exists() and img_path.stat().st_size > 0:
            self.saved_pages.add(index)
            print(f"[{self.bid}] 页 {index}/{total_pages} 已保存 ({img_path.stat().st_size} bytes)")
        else:
            print(f"[{self.bid}] 页 {index} 保存失败")

    async def _wait_for_slices_loaded(self, box_selector: str, index: int) -> bool:
        """等待 page_img_l 内所有 img 切片的 src 加载完毕"""
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
                # 再等一下确保网络拦截已保存数据
                await asyncio.sleep(0.5)
                print(f"[{self.bid}] 页 {index} 切片已加载 ({len(imgs)} 片)")
                return True

            await asyncio.sleep(0.5)

        return False

    async def _get_page_layout(self, box_selector: str) -> dict | None:
        """通过 JS 获取页面布局信息（尺寸、旋转角度、各切片 src 和位置）"""
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

            // 解析 plg 的 transform matrix 得到精确旋转角度
            let rotation = 0;
            const plg = box.querySelector('.plg');
            if (plg) {
                const plgStyle = plg.getAttribute('style') || '';
                // matrix(a, b, c, d, tx, ty)
                const matrixMatch = plgStyle.match(/matrix\\(([^)]+)\\)/);
                if (matrixMatch) {
                    const vals = matrixMatch[1].split(',').map(v => parseFloat(v.trim()));
                    const a = vals[0], b = vals[1];
                    // atan2(b, a) 得到弧度，转换为角度
                    let angle = Math.round(Math.atan2(b, a) * 180 / Math.PI);
                    if (angle < 0) angle += 360;
                    rotation = angle;
                }
                // 也检查 rotate() 写法
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
        """用 Pillow 在 Python 侧拼接切片"""
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

        # 根据 plg 的 transform 旋转修正
        if rotation == 180:
            canvas = canvas.rotate(180, expand=False)
        elif rotation == 90:
            canvas = canvas.rotate(-90, expand=True)
        elif rotation == 270:
            canvas = canvas.rotate(-270, expand=True)

        canvas.save(str(img_path), "PNG")

    def _find_cached_image(self, src: str) -> bytes | None:
        """根据 src 在拦截缓存中查找图片数据"""
        if not src:
            return None

        # src 可能是相对路径如 /deep/page/lmg/3256452/7?k=xxx
        # 缓存 key 也是 path?query 格式
        # 直接匹配
        if src in self._image_cache:
            return self._image_cache[src]

        # 带查询参数的匹配
        parsed = urlparse(src)
        path_key = parsed.path + "?" + parsed.query if parsed.query else parsed.path
        if path_key in self._image_cache:
            return self._image_cache[path_key]

        # 模糊匹配（URL 编码差异）
        for key, data in self._image_cache.items():
            if parsed.path in key:
                return data

        return None

    async def _reload_and_wait(self):
        """刷新当前页面并等待 #pb 重新出现"""
        await self.page.reload(wait_until="domcontentloaded")
        await self.page.wait_for_selector("#pb", timeout=30000)
        print(f"[{self.bid}] 页面已刷新")

    def _compile_pdf(self, pdf_path: Path, page_indices: list[int]):
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
