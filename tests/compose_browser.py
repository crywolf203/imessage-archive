"""Capture the real Compose Web UI with generated data and disposable accounts."""

import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


output = Path("compose-proof")
output.mkdir(exist_ok=True)
url = "http://127.0.0.1:18087"

with sync_playwright() as playwright:
    browser = playwright.chromium.launch()
    context = browser.new_context(viewport={"width": 1440, "height": 960})
    page = context.new_page()
    page.goto(url + "/?conversation=runtime-pdf.html")
    page.locator('input[name="username"]').fill(os.environ["APP_USER"])
    page.locator('input[name="password"]').fill(os.environ["APP_PASSWORD"])
    page.get_by_role("button", name="Sign in", exact=True).click()
    expect(page.get_by_role("button", name="Render PDF", exact=True)).to_be_visible()
    frame = page.frame_locator("#conversationFrame")
    expect(frame.get_by_text("Runtime sample 0", exact=True)).to_be_visible()
    expect(frame.locator("img").first).to_be_visible()
    assert frame.locator("img").first.evaluate("image => image.naturalWidth") > 0
    page.get_by_role("checkbox", name="Dark mode").check()
    page.screenshot(path=str(output / "desktop-dark.png"))
    page.get_by_role("checkbox", name="Dark mode").uncheck()
    page.screenshot(path=str(output / "desktop-light.png"))
    page.get_by_role("link", name="Next", exact=True).click()
    expect(frame.get_by_text("Runtime sample 449 finalsentinel", exact=True)).to_be_attached()
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
    page.locator("#conversationFrame").scroll_into_view_if_needed()
    page.screenshot(path=str(output / "mobile.png"))
    browser.close()

print("Compose browser login, conversation navigation, images and responsive layout passed")
