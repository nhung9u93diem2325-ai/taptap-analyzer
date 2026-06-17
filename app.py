# gevent monkey patch 必须在最前面
from gevent import monkey
monkey.patch_all()

import subprocess, sys, os

# 启动时自动安装 Chromium（解决 Render 缓存被清除的问题）
def ensure_chromium():
    chromium_path = "/opt/render/.cache/ms-playwright/chromium_headless_shell-1148/chrome-linux/headless_shell"
    if not os.path.exists(chromium_path):
        print("[startup] Chromium not found, installing...", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, text=True
        )
        print(result.stdout, flush=True)
        print(result.stderr, flush=True)
        print("[startup] Chromium install done", flush=True)
    else:
        print("[startup] Chromium already installed", flush=True)

ensure_chromium()

from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from openai import OpenAI
import time, re, json, os, threading, uuid

app = Flask(__name__)
CORS(app)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
tasks = {}

# ────────────────────────────────────────
# 搜索游戏
# ────────────────────────────────────────
def search_game(page, game_name):
    if re.match(r"^\d+$", game_name.strip()):
        return game_name.strip(), f"App#{game_name.strip()}"

    import urllib.parse
    url = f"https://www.taptap.cn/search?q={urllib.parse.quote(game_name)}&type=app"
    print(f"[Search] {url}")
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(3000)

    links = page.query_selector_all("a[href*='/app/']")
    for link in links:
        href = link.get_attribute("href") or ""
        m = re.search(r"/app/(\d+)", href)
        if m:
            app_id = m.group(1)
            title = (link.inner_text() or "").strip()
            print(f"[Search] found app_id={app_id}")
            return app_id, title or game_name

    return None, None


# ────────────────────────────────────────
# 爬取评论
# ────────────────────────────────────────
def extract_reviews_from_page(page):
    """从当前页面提取所有评论"""
    reviews = {}

    links = page.query_selector_all("a[href*='/review/']")
    for el in links:
        href = el.get_attribute("href") or ""
        m = re.search(r"/review/(\d+)$", href)
        if not m:
            continue
        rid = m.group(1)
        if rid in reviews:
            continue

        content = (el.inner_text() or "").strip()
        if not content or len(content) < 3:
            continue

        author, pub_time, device, likes = "", "", "", ""
        try:
            container_text = page.evaluate("""el => {
                var p = el;
                for(var i=0;i<8;i++){
                    p = p.parentElement;
                    if(!p) break;
                    if(p.tagName==='DIV' && p.innerText.length > 20) break;
                }
                return p ? p.innerText : '';
            }""", el)
            lines = [l.strip() for l in container_text.split("\n") if l.strip()]
            for line in lines:
                if re.match(r"\d{4}/\d{1,2}/\d{1,2}", line):
                    pub_time = line
                if line.startswith("来自"):
                    device = line
                if re.match(r"^\d+$", line) and not likes:
                    likes = line

            author = page.evaluate("""el => {
                var p = el;
                for(var i=0;i<8;i++){
                    p = p.parentElement;
                    if(!p) break;
                }
                if(!p) return '';
                var links = p.querySelectorAll('a[href*="/user/"]');
                return links.length > 0 ? links[0].innerText : '';
            }""", el) or ""
            author = author.strip()
        except Exception:
            pass

        reviews[rid] = {
            "review_id": rid,
            "author":    author,
            "content":   content,
            "time":      pub_time,
            "device":    device,
            "likes":     likes,
            "url":       f"https://www.taptap.cn/review/{rid}",
        }
    return reviews


def scrape_reviews(page, app_id, review_type, task_id, label_key,
                   max_no_change=5, scroll_pause=3000):
    import urllib.parse
    mapping_encoded = urllib.parse.quote(review_type)
    url = f"https://www.taptap.cn/app/{app_id}/review?os=android&mapping={mapping_encoded}&label=0"
    print(f"[Review] {url}")

    tasks[task_id]["message"] = f"正在打开{review_type}页面..."
    page.goto(url, wait_until="networkidle", timeout=60000)
    page.wait_for_timeout(5000)

    all_reviews = {}
    no_change_count = 0
    scroll_num = 0

    while True:
        scroll_num += 1
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(scroll_pause)

        current = extract_reviews_from_page(page)
        new_count = len(current) - len(all_reviews)
        all_reviews.update(current)

        tasks[task_id]["message"] = (
            f"正在爬取{review_type} · 第{scroll_num}次滚动 · "
            f"本次新增{new_count}条 · 累计{len(all_reviews)}条"
        )
        tasks[task_id][f"{label_key}_count"] = len(all_reviews)
        print(f"  scroll={scroll_num} new={new_count} total={len(all_reviews)}")

        if new_count == 0:
            no_change_count += 1
            if no_change_count >= max_no_change:
                print(f"  连续{max_no_change}次无新数据，停止")
                break
        else:
            no_change_count = 0

    return list(all_reviews.values())


# ────────────────────────────────────────
# DeepSeek 分析
# ────────────────────────────────────────
def analyze_with_deepseek(bad_reviews, good_reviews, game_name):
    client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
    bad_text  = "\n".join([f"- {r['content']}" for r in bad_reviews[:200]])
    good_text = "\n".join([f"- {r['content']}" for r in good_reviews[:200]])

    prompt = f"""你是一名专业的游戏用户研究分析师。
以下是TapTap平台上《{game_name}》的用户评论数据，请进行深度分析并按以下结构输出JSON格式报告：

【差评数据】（共{len(bad_reviews)}条，以下为样本）：
{bad_text}

【好评数据】（共{len(good_reviews)}条，以下为样本）：
{good_text}

请输出如下JSON结构（所有字段必须存在，语言用中文）：
{{
  "summary": "一段60字以内的整体概括",
  "bad_issues": [
    {{"rank": 1, "issue": "问题名称", "desc": "详细说明", "frequency": "出现频次描述"}},
    {{"rank": 2, "issue": "问题名称", "desc": "详细说明", "frequency": "出现频次描述"}},
    {{"rank": 3, "issue": "问题名称", "desc": "详细说明", "frequency": "出现频次描述"}},
    {{"rank": 4, "issue": "问题名称", "desc": "详细说明", "frequency": "出现频次描述"}},
    {{"rank": 5, "issue": "问题名称", "desc": "详细说明", "frequency": "出现频次描述"}}
  ],
  "good_highlights": [
    {{"rank": 1, "highlight": "亮点名称", "desc": "详细说明"}},
    {{"rank": 2, "highlight": "亮点名称", "desc": "详细说明"}},
    {{"rank": 3, "highlight": "亮点名称", "desc": "详细说明"}},
    {{"rank": 4, "highlight": "亮点名称", "desc": "详细说明"}},
    {{"rank": 5, "highlight": "亮点名称", "desc": "详细说明"}}
  ],
  "emotion": {{
    "negative_ratio": 差评占比整数,
    "positive_ratio": 好评占比整数,
    "negative_keywords": ["词1","词2","词3","词4","词5"],
    "positive_keywords": ["词1","词2","词3","词4","词5"],
    "overall_sentiment": "一句话描述整体情绪倾向"
  }},
  "suggestions": [
    {{"priority": "高", "area": "改进领域", "action": "具体建议"}},
    {{"priority": "高", "area": "改进领域", "action": "具体建议"}},
    {{"priority": "中", "area": "改进领域", "action": "具体建议"}},
    {{"priority": "中", "area": "改进领域", "action": "具体建议"}},
    {{"priority": "低", "area": "改进领域", "action": "具体建议"}}
  ]
}}

只输出JSON，不要有任何其他文字。"""

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=3000,
    )
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r"^```json|^```|```$", "", raw, flags=re.MULTILINE).strip()
    return json.loads(raw)


# ────────────────────────────────────────
# 主任务线程
# ────────────────────────────────────────
def run_task(task_id, game_name, app_id_override=None):
    tasks[task_id].update({"status": "running", "step": 0, "error": None, "result": None})

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-setuid-sandbox",
                "--single-process",
                "--no-zygote",
                "--disable-accelerated-2d-canvas",
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()

        try:
            # Step 1: 搜索
            if app_id_override:
                app_id = app_id_override
                resolved_name = game_name or f"App#{app_id}"
                tasks[task_id].update({"step": 1, "message": f"使用 App ID={app_id}，跳过搜索..."})
            else:
                tasks[task_id].update({"step": 1, "message": f"正在搜索《{game_name}》..."})
                app_id, resolved_name = search_game(page, game_name)
                if not app_id:
                    raise Exception(
                        f"未找到游戏《{game_name}》。"
                        "请在 TapTap 找到游戏页面，复制网址中的数字ID后填入下方输入框"
                    )
                resolved_name = resolved_name or game_name

            # Step 2: 差评
            tasks[task_id].update({"step": 2, "message": "开始爬取差评..."})
            bad_reviews = scrape_reviews(page, app_id, "差评", task_id, "bad")

            # Step 3: 好评
            tasks[task_id].update({"step": 3, "message": "开始爬取好评..."})
            good_reviews = scrape_reviews(page, app_id, "好评", task_id, "good")

            if not bad_reviews and not good_reviews:
                raise Exception(
                    f"未能爬取到任何评论（App ID={app_id}）。请稍后重试。"
                )

            # Step 4: AI 分析
            tasks[task_id].update({
                "step": 4,
                "message": f"DeepSeek 正在分析 {len(bad_reviews)} 条差评 + {len(good_reviews)} 条好评..."
            })
            analysis = analyze_with_deepseek(bad_reviews, good_reviews, resolved_name)

            tasks[task_id].update({
                "step": 5, "status": "done", "message": "分析完成！",
                "result": {
                    "game_name": resolved_name,
                    "app_id": app_id,
                    "bad_count": len(bad_reviews),
                    "good_count": len(good_reviews),
                    "analysis": analysis,
                }
            })

        except Exception as e:
            import traceback
            traceback.print_exc()
            tasks[task_id].update({"status": "error", "error": str(e), "message": str(e)})
        finally:
            browser.close()


# ────────────────────────────────────────
# 路由
# ────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.json or {}
    game_name = (data.get("game_name") or "").strip()
    app_id_override = (data.get("app_id") or "").strip()

    if not game_name and not app_id_override:
        return jsonify({"error": "请输入游戏名称或 App ID"}), 400

    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        "status": "idle", "step": 0, "total_steps": 5,
        "message": "", "bad_count": 0, "good_count": 0,
        "result": None, "error": None,
    }
    t = threading.Thread(target=run_task, args=(task_id, game_name, app_id_override or None))
    t.daemon = True
    t.start()
    return jsonify({"task_id": task_id})

@app.route("/progress/<task_id>")
def progress(task_id):
    if task_id not in tasks:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(tasks[task_id])

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"启动成功！请用浏览器打开：http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
