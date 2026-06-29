# gevent monkey patch 必须在最前面
from gevent import monkey
monkey.patch_all()

import subprocess, sys, os

from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from openai import OpenAI
import time, re, json, threading, uuid

app = Flask(__name__)
CORS(app)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
tasks = {}

# ── 配置参数 ──
SCROLL_PAUSE_MS      = 4000
MAX_NO_CHANGE        = 5
MAX_REVIEWS          = 300
MAX_SCROLL_TOTAL     = 60
PER_TYPE_TIMEOUT_SEC = 10 * 60
TOTAL_TIMEOUT_SEC    = 20 * 60


# ❗❗关键修改：延迟安装 Chromium（避免启动卡死）
def ensure_chromium_lazy():
    try:
        import playwright
        print("[startup] Playwright imported OK", flush=True)
    except Exception as e:
        print("[startup] Playwright import issue:", e, flush=True)


# ─────────────────────────────
# 搜索游戏
# ─────────────────────────────
def search_game(page, game_name):
    import urllib.parse

    if re.match(r"^\d+$", game_name.strip()):
        return game_name.strip(), f"App#{game_name.strip()}"

    url = f"https://www.taptap.cn/search?q={urllib.parse.quote(game_name)}&type=app"
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(3000)

    links = page.query_selector_all("a[href*='/app/']")
    for link in links:
        href = link.get_attribute("href") or ""
        m = re.search(r"/app/(\d+)", href)
        if m:
            return m.group(1), (link.inner_text() or game_name)

    return None, None


# ─────────────────────────────
# 提取评论
# ─────────────────────────────
def extract_reviews_from_page(page):
    try:
        js_code = """(function() {
            var results = [];
            var seen = {};
            var links = document.querySelectorAll('a[href]');
            for (var i = 0; i < links.length; i++) {
                var el = links[i];
                var href = el.getAttribute('href') || '';
                var idx = href.indexOf('/review/');
                if (idx === -1) continue;

                var rid = href.substring(idx + 8);
                if (!/^[0-9]+$/.test(rid)) continue;
                if (seen[rid]) continue;
                seen[rid] = true;

                var content = (el.textContent || '').trim();
                if (!content) continue;

                results.push({
                    review_id: rid,
                    content: content,
                    url: 'https://www.taptap.cn/review/' + rid
                });
            }
            return results;
        })()"""

        return page.evaluate(js_code) or []
    except Exception as e:
        print("[extract error]", e, flush=True)
        return []


# ─────────────────────────────
# Flask routes
# ─────────────────────────────
@app.route("/")
def index():
    return "Service Running"

@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.json or {}
    game_name = data.get("game_name", "").strip()

    if not game_name:
        return jsonify({"error": "missing game_name"}), 400

    task_id = str(uuid.uuid4())
    tasks[task_id] = {"status": "running", "message": "starting"}

    def run():
        from playwright.sync_api import sync_playwright

        ensure_chromium_lazy()

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )

            page = browser.new_page()

            try:
                tasks[task_id]["message"] = "searching game..."
                app_id, name = search_game(page, game_name)

                if not app_id:
                    raise Exception("Game not found")

                tasks[task_id]["message"] = "done"
                tasks[task_id]["result"] = {
                    "app_id": app_id,
                    "name": name
                }
                tasks[task_id]["status"] = "done"

            except Exception as e:
                tasks[task_id]["status"] = "error"
                tasks[task_id]["message"] = str(e)

            finally:
                browser.close()

    threading.Thread(target=run, daemon=True).start()

    return jsonify({"task_id": task_id})


@app.route("/progress/<task_id>")
def progress(task_id):
    return jsonify(tasks.get(task_id, {"error": "not found"}))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)