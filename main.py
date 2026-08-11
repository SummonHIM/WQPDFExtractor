import asyncio
import re
from pathlib import Path

from playwright.async_api import async_playwright, BrowserContext, Page

from extractor import BookExtractor

OUTPUT_DIR = Path(__file__).parent / "output"
BOOKSHELF_URL = "https://wqbook.wqxuetang.com/user/userbookshelf"
PDF_URL_PATTERN = re.compile(r"https://[^/]*\.wqxuetang\.com/deep/read/pdf")

DISABLE_DEVTOOL_BYPASS = """
// 绕过 disable-devtool 检测
Object.defineProperty(window, 'outerWidth', { get: () => window.innerWidth });
Object.defineProperty(window, 'outerHeight', { get: () => window.innerHeight });
// 阻止 debugger 语句
const originalFunction = Function.prototype.constructor;
Function.prototype.constructor = function(...args) {
    if (args.length > 0 && typeof args[args.length - 1] === 'string' && args[args.length - 1].includes('debugger')) {
        return function() {};
    }
    return originalFunction.apply(this, args);
};
// 覆盖 setInterval/setTimeout 中的检测
const _setInterval = window.setInterval;
window.setInterval = function(fn, delay, ...args) {
    const fnStr = typeof fn === 'function' ? fn.toString() : String(fn);
    if (fnStr.includes('debugger') || fnStr.includes('devtool') || fnStr.includes('DisableDevtool')) {
        return 0;
    }
    return _setInterval.call(this, fn, delay, ...args);
};
const _setTimeout = window.setTimeout;
window.setTimeout = function(fn, delay, ...args) {
    const fnStr = typeof fn === 'function' ? fn.toString() : String(fn);
    if (fnStr.includes('debugger') || fnStr.includes('devtool') || fnStr.includes('DisableDevtool')) {
        return 0;
    }
    return _setTimeout.call(this, fn, delay, ...args);
};
// 阻止页面关闭
window.close = function() {};
// 阻止通过 console 检测
const _consoleLog = console.log;
Object.defineProperty(console, '_commandLineAPI', { get: () => undefined });
"""


async def handle_new_page(page: Page, tasks: dict[str, asyncio.Task], completed: set[str]):
    url = page.url
    if not PDF_URL_PATTERN.search(url):
        return

    page_key = str(id(page))

    if page_key in completed:
        return
    if page_key in tasks and not tasks[page_key].done():
        return

    bid_match = re.search(r"bid=(\d+)", url)
    bid = bid_match.group(1) if bid_match else "unknown"

    print(f"[{bid}] 检测到新的PDF页面: {url}")
    extractor = BookExtractor(page, OUTPUT_DIR, page_key)
    task = asyncio.create_task(run_extractor(extractor, bid, page_key, tasks, completed))
    tasks[page_key] = task


async def run_extractor(extractor: BookExtractor, bid: str, page_key: str, tasks: dict, completed: set):
    try:
        await extractor.extract()
        print(f"[{bid}] 提取完成")
    except Exception as e:
        print(f"[{bid}] 提取失败: {e}")
    finally:
        completed.add(page_key)
        tasks.pop(page_key, None)


async def monitor_pages(context: BrowserContext, tasks: dict[str, asyncio.Task], completed: set[str]):
    while True:
        for page in context.pages:
            url = page.url
            if PDF_URL_PATTERN.search(url):
                page_key = str(id(page))
                if page_key not in completed and page_key not in tasks:
                    await handle_new_page(page, tasks, completed)
        await asyncio.sleep(2)


async def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    user_data_dir = Path(__file__).parent / "browser_data"

    print("启动浏览器...")
    print(f"输出目录: {OUTPUT_DIR}")
    print()

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-web-security",
            ],
        )

        await context.add_init_script(DISABLE_DEVTOOL_BYPASS)

        page = context.pages[0] if context.pages else await context.new_page()
        await page.goto(BOOKSHELF_URL)
        print(f"已打开书架页面: {BOOKSHELF_URL}")
        print("请登录后打开需要提取的PDF页面，程序会自动开始提取。")
        print("支持同时打开多个PDF页面并行提取。")
        print("关闭浏览器窗口即可退出程序。")
        print()

        tasks: dict[str, asyncio.Task] = {}
        completed: set[str] = set()

        context.on("page", lambda new_page: asyncio.ensure_future(
            on_page_ready(new_page, tasks, completed)
        ))

        monitor_task = asyncio.create_task(monitor_pages(context, tasks, completed))

        try:
            await context.pages[0].wait_for_event("close", timeout=0)
        except Exception:
            pass

        monitor_task.cancel()
        for task in tasks.values():
            task.cancel()

        print("浏览器已关闭，程序退出。")


async def on_page_ready(page: Page, tasks: dict[str, asyncio.Task], completed: set[str]):
    try:
        await page.wait_for_load_state("domcontentloaded")
        await handle_new_page(page, tasks, completed)
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
