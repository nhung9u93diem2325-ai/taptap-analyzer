from gevent import monkey
monkey.patch_all()

import os
import time
import re
import json
import uuid
import threading
import urllib.parse

from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
from playwright.sync_api import sync_playwright
from openai import OpenAI

app = Flask(__name__)
CORS(app)

# =========================
# 配置（重点优化）
# =========================
BAD_TARGET = 100     # 差评目标
GOOD_TARGET = 120    # 好评目标
MAX_SCROLL = 40
SCROLL_PAUSE = 2.5

tasks = {}

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# =========================
# 搜索游戏
# =========================
def search_game(page, game_name):
    if re.match(r"^\d+$", game_name.strip()):
        return game_name.strip(), f"App#{game_name.strip()}"

    url = f"https://www.taptap.cn/search?q={urllib.parse.quote(game_name)}&type=app"
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(2000)

    links = page.query_selector_all("a[href*='/app/']")
    for link in links:
        href = link.get_attribute("href") or ""
        m = re.search(r"/app/(\d+)", href)
        if m:
            return m.group(1), (link.inner_text() or game_name)

    return None, None


# =========================
# 更稳定评论抓取（去DOM diff）
# =========================
def extract_reviews(page):
    js = """
    () => {
        const nodes = document.querySelectorAll('a[href*="/review/"]');
        const results = [];
        const seen = new Set();

        nodes.forEach(n => {
            const href = n.getAttribute('href') || '';
            const m = href.match(/\\/review\\/(\\d+)/);
            if (!m) return;

            const id = m[1];
            if (seen.has(id)) return;
            seen.add(id);

            const text = (n.innerText || '').trim();
            if (!text) return;

            results.push({
                id: id,
                content: text,
                url: 'https://www.taptap.cn/review/' + id
            });
        });

        return results;
    }
    """
    try:
        return page.evaluate(js) or []
    except:
        return []


# =========================
# 爬取核心（已修复卡死）
# =========================
def crawl_reviews(page, app_id, label, target):
    url = f"https://www.taptap.cn/app/{app_id}/review?os=android&mapping={label}&label=0"
    page.goto(url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(3000)

    reviews = {}
    no_change = 0

    for i in range(MAX_SCROLL):

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(int(SCROLL_PAUSE * 1000))

        items = extract_reviews(page)

        before = len(reviews)
        for r in items:
            reviews[r["id"]] = r
        after = len(reviews)

        print(f"[{label}] scroll={i} total={after}")

        # ✅ 达到目标直接停止（核心优化）
        if len(reviews) >= target:
            break

        # ❗无新增检测
        if after == before:
            no_change += 1
        else:
            no_change = 0

        if no_change >= 8:
            break

    return list(reviews.values())


# =========================
# AI分析
# =========================
def analyze(bad, good, name):
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com"
    )

    prompt = f"""
你是游戏分析师，请分析《{name}》。

差评：{len(bad)}条
好评：{len(good)}条

输出JSON：
- summary
- bad_issues(5)
- good_highlights(5)
- emotion
- suggestions
只输出JSON
"""

    res = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
    )

    return json.loads(res.choices[0].message.content)


# =========================
# 后台任务
# =========================
def run_task(task_id, game_name):
    tasks[task_id] = {"status": "running", "message": "starting"}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )

        page = browser.new_page()

        try:
            tasks[task_id]["message"] = "searching game"

            app_id, name = search_game(page, game_name)
            if not app_id:
                raise Exception("Game not found")

            tasks[task_id]["message"] = "crawl bad reviews"

            bad = crawl_reviews(page, app_id, "差评", BAD_TARGET)

            tasks[task_id]["message"] = "crawl good reviews"

            good = crawl_reviews(page, app_id, "好评", GOOD_TARGET)

            tasks[task_id]["message"] = "analyzing"

            result = analyze(bad, good, name)

            tasks[task_id] = {
                "status": "done",
                "game": name,
                "app_id": app_id,
                "bad": len(bad),
                "good": len(good),
                "analysis": result
            }

        except Exception as e:
            tasks[task_id] = {
                "status": "error",
                "message": str(e)
            }

        finally:
            browser.close()


# =========================
# API
# =========================
@app.route("/")
def home():
    return "OK - TapTap Scraper Running"


@app.route("/analyze", methods=["POST"])
def analyze_api():
    data = request.json or {}
    game = data.get("game_name", "").strip()

    if not game:
        return jsonify({"error": "missing game_name"}), 400

    task_id = str(uuid.uuid4())

    t = threading.Thread(
        target=run_task,
        args=(task_id, game),
        daemon=True
    )
    t.start()

    return jsonify({"task_id": task_id})


@app.route("/progress/<task_id>")
def progress(task_id):
    return jsonify(tasks.get(task_id, {"error": "not found"}))


# =========================
# main
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
