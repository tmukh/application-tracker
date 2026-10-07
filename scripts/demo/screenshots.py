"""Take the README screenshots and the short walkthrough recording from the demo server (dark mode).

    python scripts/demo/screenshots.py http://127.0.0.1:5077 docs/images

Needs: pip install playwright && playwright install chromium, and ffmpeg for the GIF.
"""
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

base = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:5077").rstrip("/")
out = Path(sys.argv[2] if len(sys.argv) > 2 else "docs/images")
out.mkdir(parents=True, exist_ok=True)
DESKTOP = {"width": 1180, "height": 760}


def app_id(page, company):
    page.goto(base + "/")
    href = page.locator("a", has_text=company).first.get_attribute("href")
    return href


with sync_playwright() as p:
    browser = p.chromium.launch()
    # ---- still screenshots
    ctx = browser.new_context(viewport=DESKTOP, color_scheme="dark", device_scale_factor=2)
    page = ctx.new_page()
    page.goto(base + "/")
    page.wait_for_timeout(600)
    page.screenshot(path=out / "dashboard.png", full_page=True)
    page.goto(base + app_id(page, "Northwind Labs"))
    page.wait_for_timeout(400)
    page.screenshot(path=out / "application.png", full_page=True)
    page.goto(base + "/review")
    page.wait_for_timeout(400)
    page.screenshot(path=out / "review.png", full_page=True)
    page.goto(base + "/settings")
    page.wait_for_timeout(400)
    page.screenshot(path=out / "settings.png", full_page=True)
    ctx.close()

    phone = browser.new_context(viewport={"width": 390, "height": 844}, color_scheme="dark", device_scale_factor=3,
                                is_mobile=True, has_touch=True)
    page = phone.new_page()
    page.goto(base + "/")
    page.wait_for_timeout(600)
    page.screenshot(path=out / "mobile.png", full_page=False)
    phone.close()

    # ---- walkthrough recording
    tmp = Path(tempfile.mkdtemp())
    ctx = browser.new_context(viewport=DESKTOP, color_scheme="dark", record_video_dir=str(tmp), record_video_size=DESKTOP)
    page = ctx.new_page()
    page.goto(base + "/")
    page.wait_for_timeout(1500)
    page.locator("a.chip.interview").first.click()           # filter: interviews
    page.wait_for_timeout(1500)
    page.locator("a", has_text="Northwind Labs").first.click()  # open one application
    page.wait_for_timeout(2200)
    page.go_back()
    page.wait_for_timeout(600)
    page.locator("a.nav", has_text="Review").click()          # the few mails that need a decision
    page.wait_for_timeout(2200)
    page.locator("a.nav", has_text="Settings").click()        # everything is set up here
    page.wait_for_timeout(1500)
    page.mouse.wheel(0, 700)
    page.wait_for_timeout(1500)
    page.mouse.wheel(0, -700)
    page.wait_for_timeout(800)
    ctx.close()  # finalizes the video
    browser.close()

video = next(tmp.glob("*.webm"))
shutil.copy(video, out / "walkthrough.webm")
if shutil.which("ffmpeg"):
    pal = tmp / "palette.png"
    vf = "fps=8,scale=840:-1:flags=lanczos"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vf", f"{vf},palettegen=max_colors=96", str(pal)], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(pal), "-lavfi", f"{vf}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4",
                    str(out / "walkthrough.gif")], check=True)
print("written:", sorted(f.name for f in out.iterdir()))
