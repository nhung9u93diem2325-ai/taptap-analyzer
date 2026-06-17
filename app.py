# gevent monkey patch 必须在最前面
from gevent import monkey
monkey.patch_all()

import subprocess, sys, os

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

# ── 超时参数 ──
SCROLL_PAUSE_MS      = 4000   # 每次滚动等待 4 秒
MAX_NO_CHANGE        = 5      # 连续 5 次无新增则停止
MAX_REVIEWS          = 300    # 每类最多爬取条数
MAX_SCROLL_TOTAL     = 60     # 最多滚动 60 次
PER_TYPE_TIMEOUT_SEC = 10 * 60  # 单类评论超时：10 分钟
TOTAL_TIMEOUT_SEC    = 20 * 60  # 整体任务超时：20 分钟

# ────────────────────────────────────────
# 搜索游戏
# ────────────────────────────────────────
def search_game(page, game_name):
    if re.match(r"^\d+$", game_name.strip()):
        return game_name.strip(), f"App#{game_name.strip()}"

    import urllib.parse
    url = f"https://www.taptap.cn/search?q={urllib.parse.quote(game_name)}&type=app"
    print(f"[Search] {url}", flush=True)
    page.goto(url, wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(3000)

    links = page.query_selector_all("a[href*='/app/']")
    for link in links:
        href = link.get_attribute("href") or ""
        m = re.search(r"/app/(\d+)", href)
        if m:
            app_id = m.group(1)
            title = (link.inner_text() or "").strip()
            print(f"[Search] found app_id={app_id}", flush=True)
            return app_id, title or game_name

    return None, None


# ────────────────────────────────────────
# 从页面提取评论
# ────────────────────────────────────────
def extract_reviews_from_page(page):
    reviews = {}
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
    var afterSlash = href.substring(idx + 8);
    var isNum = true;
    for (var ci = 0; ci < afterSlash.length; ci++) {
      var cc = afterSlash.charCodeAt(ci);
      if (cc < 48 || cc > 57) { isNum = false; break; }
    }
    if (!isNum || afterSlash.length === 0) continue;
    var rid = afterSlash;
    if (seen[rid]) continue;
    seen[rid] = true;
    var content = (el.textContent || '').trim();
    if (!content || content.length < 3) continue;
    var author = '', pub_time = '', device = '';
    var p = el;
    for (var j = 0; j < 8; j++) {
      if (!p.parentElement) break;
      p = p.parentElement;
      if (p.tagName === 'DIV' && (p.textContent || '').length > 20) break;
    }
    if (p) {
      var lines = (p.innerText || p.textContent || '').split('\n');
      for (var k = 0; k < lines.length; k++) {
        var line = lines[k].trim();
        if (!line) continue;
        var c0 = line.charCodeAt(0), c1 = line.charCodeAt(1),
            c2 = line.charCodeAt(2), c3 = line.charCodeAt(3);
        if (c0>=48&&c0<=57&&c1>=48&&c1<=57&&c2>=48&&c2<=57&&c3>=48&&c3<=57) pub_time = line;
        if (c0===26469&&c1===33258) device = line;
      }
      var ulinks = p.querySelectorAll('a[href*="/user/"]');
      if (ulinks.length > 0) author = (ulinks[0].textContent || '').trim();
    }
    results.push({review_id: rid, author: author, content: content,
                  time: pub_time, device: device, likes: '',
                  url: 'https://www.taptap.cn/review/' + rid});
  }
  return results;
})()"""
        items = page.evaluate(js_code)
        for item in (items or []):
            rid = item.get("review_id", "")
            if rid and item.get("content"):
                reviews[rid] = item
    except Exception as e:
        print(f"[extract] error: {e}", flush=True)
    return reviews


# ────────────────────────────────────────
# 滚动爬取评论（10分钟超时后强制返回已采集数据）
# ────────────────────────────────────────
def scrape_reviews(page, app_id, review_type, task_id, label_key):
    import urllib.parse
    url = (f"https://www.taptap.cn/app/{app_id}/review"
           f"?os=android&mapping={urllib.parse.quote(review_type)}&label=0")
    print(f"[Review] {url}", flush=True)

    tasks[task_id]["message"] = f"正在打开{review_type}页面..."
    try:
        page.goto(url, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(5000)
    except Exception as e:
        print(f"[Review] goto error: {e}", flush=True)

    all_reviews = {}
    no_change_count = 0
    scroll_num = 0
    start_time = time.time()

    while True:
        elapsed = time.time() - start_time

        # ── 10分钟超时：强制结束，带走已有数据进行分析 ──
        if elapsed >= PER_TYPE_TIMEOUT_SEC:
            print(f"  [{review_type}] ⏰ 超过10分钟，强制结束，已采集{len(all_reviews)}条", flush=True)
            tasks[task_id]["message"] = (
                f"⏰ {review_type}爬取已达10分钟上限，"
                f"共采集{len(all_reviews)}条，进入分析..."
            )
            break

        # ── 数量上限 ──
        if len(all_reviews) >= MAX_REVIEWS:
            print(f"  [{review_type}] 已达上限{MAX_REVIEWS}条，停止", flush=True)
            tasks[task_id]["message"] = f"{review_type}已采集{MAX_REVIEWS}条，进入下一步..."
            break

        # ── 滚动次数上限 ──
        if scroll_num >= MAX_SCROLL_TOTAL:
            print(f"  [{review_type}] 已滚动{MAX_SCROLL_TOTAL}次，停止", flush=True)
            break

        scroll_num += 1
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(SCROLL_PAUSE_MS)

        current = extract_reviews_from_page(page)
        new_count = len(current) - len(all_reviews)
        all_reviews.update(current)

        elapsed_min = int(elapsed // 60)
        elapsed_sec = int(elapsed % 60)
        remain_sec = max(0, int(PER_TYPE_TIMEOUT_SEC - elapsed))
        remain_min = remain_sec // 60
        remain_s   = remain_sec % 60

        tasks[task_id]["message"] = (
            f"正在爬取{review_type} · 第{scroll_num}次 · "
            f"新增{new_count}条 · 累计{len(all_reviews)}条 · "
            f"剩余{remain_min}m{remain_s:02d}s"
        )
        tasks[task_id][f"{label_key}_count"] = len(all_reviews)
        print(f"  [{review_type}] scroll={scroll_num} new={new_count} "
              f"total={len(all_reviews)} elapsed={elapsed:.0f}s", flush=True)

        if new_count == 0:
            no_change_count += 1
            if no_change_count >= MAX_NO_CHANGE:
                print(f"  [{review_type}] 连续{MAX_NO_CHANGE}次无新数据，自然结束", flush=True)
                tasks[task_id]["message"] = (
                    f"{review_type}已爬取完毕，共{len(all_reviews)}条，进入下一步..."
                )
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
# 主任务线程（含20分钟整体超时）
# ────────────────────────────────────────
def run_task(task_id, game_name, app_id_override=None):
    tasks[task_id].update({"status": "running", "step": 0, "error": None, "result": None})
    task_start = time.time()

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
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 900},
        )
        page = context.new_page()

        try:
            # ── Step 1: 搜索 ──
            if app_id_override:
                app_id = app_id_override
                resolved_name = game_name or f"App#{app_id}"
                tasks[task_id].update({"step": 1, "message": f"使用 App ID={app_id}，跳过搜索..."})
            else:
                tasks[task_id].update({"step": 1, "message": f"正在搜索《{game_name}》..."})
                app_id, resolved_name = search_game(page, game_name)
                if not app_id:
                    raise Exception(
                        f"未找到游戏《{game_name}》，请在 TapTap 找到游戏页面，"
                        "复制网址中的数字 ID 填入输入框"
                    )
                resolved_name = resolved_name or game_name

            # ── 整体超时检查 ──
            def check_total_timeout():
                if time.time() - task_start >= TOTAL_TIMEOUT_SEC:
                    raise Exception("整体任务超过20分钟，已强制终止并返回已有结果")

            # ── Step 2: 差评 ──
            check_total_timeout()
            tasks[task_id].update({"step": 2, "message": "开始爬取差评（最多10分钟）..."})
            bad_reviews = scrape_reviews(page, app_id, "差评", task_id, "bad")
            print(f"[Task] 差评完成，共{len(bad_reviews)}条", flush=True)

            # ── Step 3: 好评 ──
            check_total_timeout()
            tasks[task_id].update({"step": 3, "message": "开始爬取好评（最多10分钟）..."})
            good_reviews = scrape_reviews(page, app_id, "好评", task_id, "good")
            print(f"[Task] 好评完成，共{len(good_reviews)}条", flush=True)

            if not bad_reviews and not good_reviews:
                raise Exception(f"未能爬取到任何评论（App ID={app_id}），请稍后重试。")

            # ── Step 4: AI 分析 ──
            tasks[task_id].update({
                "step": 4,
                "message": (
                    f"✨ DeepSeek 正在分析 {len(bad_reviews)} 条差评 "
                    f"+ {len(good_reviews)} 条好评，请稍候..."
                )
            })
            print(f"[Task] 开始 DeepSeek 分析", flush=True)
            analysis = analyze_with_deepseek(bad_reviews, good_reviews, resolved_name)
            print(f"[Task] DeepSeek 分析完成", flush=True)

            tasks[task_id].update({
                "step": 5, "status": "done", "message": "🎉 分析完成！",
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
            tasks[task_id].update({"status": "error", "error": str(e), "message": f"❌ {str(e)}"})
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
    t = threading.Thread(
        target=run_task,
        args=(task_id, game_name, app_id_override or None)
    )
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
