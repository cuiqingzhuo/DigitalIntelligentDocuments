"""Exercise the browser UI and its API against a disposable local instance."""

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def smoke(url, browser_path, evidence):
    with sync_playwright() as driver:
        browser = driver.chromium.launch(executable_path=browser_path, headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1050}, locale="zh-CN")
        page = context.new_page()
        errors = []
        for event in ("pageerror",):
            page.on(event, lambda error: errors.append(str(error)))
        page.goto(url)
        expect(page.locator("#network")).to_have_text("演示馆 · 在线")
        expect(page.locator("#catalog-time")).to_contain_text("缓存于")
        if evidence:
            page.screenshot(path=str(evidence/"reader.png"), full_page=True)
        page.get_by_role("button", name="数据结构", exact=True).click()
        expect(page.get_by_role("heading", name="匹配版本 · 2 条")).to_be_visible()
        page.get_by_role("button", name="选择 数据结构 第3版").click()
        page.get_by_role("button", name="确认版本").click()
        page.locator("#route-face").select_option("A01-A")
        expect(page.locator("#route-status")).to_contain_text("主入口 → A01-A")
        page.locator("#shelf-marker").fill("A01-B")
        page.get_by_role("button", name="确认架面").click()
        expect(page.locator("#arrival-message")).to_contain_text("实际扫码位置为 A01-B")
        page.locator("#shelf-marker").fill("A01-A")
        page.get_by_role("button", name="确认架面").click()
        expect(page.get_by_role("heading", name="拍照定位")).to_be_visible()
        page.get_by_role("button", name="目标在画面内").click()
        page.get_by_role("button", name="识别书脊").click()
        expect(page.locator("#recognition-result")).to_contain_text("画面左起第 3 本", timeout=45_000)
        expect(page.locator("#overlays polygon")).to_have_count(1)
        if evidence:
            page.locator("#capture").screenshot(path=str(evidence/"recognition.png"))
        page.locator("#barcode").fill("DIW000002")
        page.get_by_role("button", name="核对", exact=True).click()
        expect(page.locator("#verification-result")).to_contain_text("拿到的版本或复本与目标不符")
        page.locator("#barcode").fill("DIW000009")
        page.get_by_role("button", name="核对", exact=True).click()
        expect(page.locator("#verification-result")).to_contain_text("最终 已核对正确")
        for name, slug in (("只有同名异版", "wrong-edition"), ("目标不在画面", "absent")):
            page.get_by_role("button", name=name).click()
            page.get_by_role("button", name="识别书脊").click()
            expect(page.locator("#recognition-result")).to_contain_text("本次未识别", timeout=45_000)
            expect(page.locator("#overlays polygon")).to_have_count(0)
            if evidence:
                page.locator("#capture").screenshot(path=str(evidence/f"recognition-{slug}.png"))
        page.locator("#reason").select_option("unrecognized")
        page.locator("#feedback-note").fill("浏览器验收 · 自建书架")
        context.set_offline(True)
        page.get_by_role("button", name="提交反馈").click()
        expect(page.locator("#feedback-result")).to_contain_text("已离线排队")
        page.get_by_role("button", name="提交反馈").click()
        expect(page.locator("#network")).to_contain_text("1 项待同步")
        context.set_offline(False)
        expect(page.locator("#feedback-result")).to_contain_text("待接单")
        staff = context.new_page()
        staff.on("pageerror", lambda error: errors.append(str(error)))
        staff.goto(url+"/workbench")
        staff.get_by_role("button", name="馆员", exact=True).click()
        expect(staff.locator("#order-list tbody tr")).to_have_count(1)
        staff.get_by_role("button", name="打开").click()
        staff.get_by_role("button", name="保存工单").click()
        expect(staff.locator("#order-list")).to_contain_text("已接单")
        staff.get_by_role("button", name="打开").click()
        staff.locator("#order-status").select_option("closed")
        staff.locator("#order-finding").select_option("on_shelf")
        staff.locator("#order-receipt").fill("已现场核实")
        staff.get_by_role("button", name="保存工单").click()
        expect(staff.locator("#order-list")).to_contain_text("已关闭")
        page.get_by_role("button", name="刷新工单回执").click()
        expect(page.locator("#feedback-result")).to_contain_text("馆员已核实在架")
        staff.get_by_role("button", name="退出登录").click()
        staff.get_by_role("button", name="管理员", exact=True).click()
        staff.get_by_role("button", name="地图与架标").click()
        expect(staff.locator("#map-version")).to_contain_text("已发布 v1")
        staff.locator("#map-name").fill("一楼开架演示")
        staff.get_by_role("button", name="保存草稿并预览").click()
        expect(staff.locator("#map-version")).to_contain_text("草稿 v2")
        staff.get_by_role("button", name="发布当前草稿").click()
        expect(staff.locator("#map-version")).to_contain_text("已发布 v2")
        staff.locator("#qr-face").fill("A01-A")
        staff.get_by_role("button", name="生成二维码").click()
        expect(staff.get_by_alt_text("A01-A 架标二维码")).to_be_visible()
        if evidence:
            staff.screenshot(path=str(evidence/"workbench.png"), full_page=True)
        assert not errors, errors
        mobile = browser.new_page(viewport={"width": 360, "height": 760}, is_mobile=True, device_scale_factor=2)
        mobile.goto(url)
        expect(mobile.locator("#network")).to_have_text("演示馆 · 在线")
        assert mobile.evaluate("document.documentElement.scrollWidth === innerWidth")
        browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", help="Edge or Chrome executable for environments without Playwright Chromium")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    if args.evidence:
        args.evidence.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=ROOT/"runtime", prefix="browser-") as working:
        with socket.socket() as temporary:
            temporary.bind(("127.0.0.1",0))
            port = temporary.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        env = {**os.environ, "DIW_RUNTIME":working, "DIW_DEMO":"1", "DIW_DATABASE_URL":"",
               "DIW_PUBLIC_LIBRARY":"demo", "DIW_PUBLIC_FLOOR":"F1"}
        env.pop("DIW_DATABASE_URL")
        log = Path(working)/"server.log"
        try:
            with log.open("w", encoding="utf-8") as output:
                process = subprocess.Popen([sys.executable,"-m","uvicorn","app.main:app","--host","127.0.0.1",
                                            "--port",str(port)], env=env, cwd=ROOT, stdout=output, stderr=output)
                try:
                    for _ in range(60):
                        try:
                            with urlopen(url+"/health",timeout=.8):
                                break
                        except OSError:
                            if process.poll() is not None:
                                raise RuntimeError("Demo server exited early")
                            time.sleep(.25)
                    else:
                        raise RuntimeError("Demo server did not start")
                    smoke(url,args.browser,args.evidence)
                finally:
                    process.terminate()
                    process.wait(timeout=10)
        except BaseException:
            print(log.read_text(encoding="utf-8",errors="replace"))
            raise
    print("Browser smoke passed: catalog, route, recognition, verification, offline retry, staff receipt and map publish.")


if __name__ == "__main__":
    (ROOT/"runtime").mkdir(exist_ok=True)
    main()
