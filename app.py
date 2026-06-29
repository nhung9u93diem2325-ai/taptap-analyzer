from gevent import monkey
monkey.patch_all()

import os
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

# =====================
# 配置
# =====================
BAD_TARGET = 100
GOOD_TARGET = 120
MAX_SCROLL = 35
SCROLL_PAUSE = 2.5

tasks = {}

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# =====================
# 首页（前端）
# =====================
@app.route("/")
def home():
    return render_template("index.html")


# =====================
# 防呆API（解决400）
# =====================
@app.route("/analyze", methods=["POST"])
def analyze_api():
    data = request.get_json(force=True, silent=True) or {}

    game = (data.get("game_name") or "").strip()

    if not game:
        return jsonify({
            "error": "missing game_name",
            "hint": "frontend must send JSON: {game_name: 'xxx'}"
        }), 400

    task_id = str(uuid.uuid4())

    tasks[task_id] = {
        "status": "running",
        "message": "queued"
    }

    thread = threading.Thread(target=run_task, args=(task_id, game))
    thread.start()

    return jsonify({"task_id": task_id})


# =====================
# 进度查询
# =====================
@app.route("/progress/<task_id>")
def progress(task_id):
    return jsonify(tasks.get(task_id, {"error": "not found"}))


# =====================
# 搜索游戏
# =====================
def search_game(page, game_name):
    if re.match(r"^\d+$", game_name):
        return game_name, f"App#{game_name}"

    url = f"https://www.taptap.cn/search?q={urllib.parse.quote(game_name)}&type=app"
    page.goto(url, wait_until="networkidle")
    page.wait_for_timeout(2500)

    links = page.query_selector_all("a[href*='/app/']")

    for l in links:
        href = l.get_attribute("href") or ""
        m = re.search(r"/app/(\d+)", href)
        if m:
            return m.group(1), game_name

    return None, None


# =====================
# 抓评论（稳定版）
# =====================
def extract_reviews(page):
    js = """
    () => {
        const nodes = document.querySelectorAll('a[href*="/review/"]');
        const out = [];
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

            out.push({ id, content: text });
        });

        return out;
    }
    """
    try:
        return page.evaluate(js) or []
    except:
        return []


# =====================
# 爬虫核心
# =====================
def crawl_reviews(page, app_id, label, target):
    url = f"https://www.taptap.cn/app/{app_id}/review?mapping={label}&label=0"

    page.goto(url, wait_until="networkidle")
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

        print(f"[{label}] {i} -> {after}")

        if len(reviews) >= target:
            break

        if after == before:
            no_change += 1
        else:
            no_change = 0

        if no_change >= 8:
            break

    return list(reviews.values())


# =====================
# AI分析
# =====================
def analyze(bad, good, name):
    client = OpenAI(
        api_key=DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com"
    )

    prompt = f"""
分析游戏：《{name}》

差评：{len(bad)}
好评：{len(good)}

输出JSON：
summary / issues / highlights / suggestions
"""

    res = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3
    )

    return json.loads(res.choices[0].message.content)


# =====================
# 后台任务
# =====================
def run_task(task_id, game_name):
    tasks[task_id] = {"status": "running", "message": "starting"}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"]
            )

            page = browser.new_page()

            tasks[task_id]["message"] = "search game"

            app_id, name = search_game(page, game_name)

            if not app_id:
                raise Exception("Game not found")

            tasks[task_id]["message"] = "crawl bad"

            bad = crawl_reviews(page, app_id, "差评", BAD_TARGET)

            tasks[task_id]["message"] = "crawl good"

            good = crawl_reviews(page, app_id, "好评", GOOD_TARGET)

            tasks[task_id]["message"] = "analyze"

            result = analyze(bad, good, name)

            tasks[task_id] = {
                "status": "done",
                "game": name,
                "app_id": app_id,
                "bad": len(bad),
                "good": len(good),
                "analysis": result
            }

            browser.close()

    except Exception as e:
        tasks[task_id] = {
            "status": "error",
            "message": str(e)
        }


# =====================
# main
# =====================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
