from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from openai import OpenAI
import time, re, json, os, threading

app = Flask(__name__)
CORS(app)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.taptap.cn/",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── 全局任务状态（每个任务用 task_id 隔离） ──
tasks = {}

# ────────────────────────────────────────
# 搜索游戏，返回 app_id
# ────────────────────────────────────────
def search_game(game_name):
    url = f"https://www.taptap.cn/search?q={requests.utils.quote(game_name)}&type=app"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(resp.text, "html.parser")
        # 找第一个 /app/数字 链接
        for a in soup.find_all("a", href=True):
            m = re.search(r"/app/(\d+)", a["href"])
            if m:
                return m.group(1)
    except Exception as e:
        print(f"搜索出错: {e}")
    return None

# ────────────────────────────────────────
# 爬取单页评论（HTML解析）
# ────────────────────────────────────────
def fetch_review_page(app_id, mapping, page=1):
    """
    TapTap 评论页 URL 格式：
    https://www.taptap.cn/app/{app_id}/review?os=android&mapping={mapping}&label=0&page={page}
    """
    url = f"https://www.taptap.cn/app/{app_id}/review"
    params = {
        "os": "android",
        "mapping": mapping,
        "label": "0",
        "page": page,
    }
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=20)
        return resp.text
    except Exception as e:
        print(f"请求失败 page={page}: {e}")
        return ""

def parse_page(html):
    """从 HTML 中提取评论，返回列表"""
    soup = BeautifulSoup(html, "html.parser")
    reviews = []
    seen = set()

    # TapTap 评论链接格式 /review/数字
    for link in soup.find_all("a", href=re.compile(r"/review/\d+$")):
        href = link.get("href", "")
        m = re.search(r"/review/(\d+)", href)
        if not m:
            continue
        rid = m.group(1)
        if rid in seen:
            continue
        seen.add(rid)

        content = link.get_text(separator=" ", strip=True)
        if not content or len(content) < 3:
            continue

        # 向上找父容器
        author, pub_time, device = "", "", ""
        container = link.find_parent("div")
        if container:
            # 作者
            user_link = container.find("a", href=re.compile(r"/user/\d+"))
            if user_link:
                author = user_link.get_text(strip=True)
            # 时间
            t = container.find(string=re.compile(r"\d{4}/\d{1,2}/\d{1,2}"))
            if t:
                pub_time = str(t).strip()
            # 设备
            d = container.find(string=re.compile(r"来自"))
            if d:
                device = str(d).strip()

        reviews.append({
            "review_id": rid,
            "author": author,
            "content": content,
            "time": pub_time,
            "device": device,
            "url": "https://www.taptap.cn" + href,
        })
    return reviews

def scrape_all_reviews(app_id, mapping, task_id, label_key, max_pages=50):
    """翻页爬取所有评论"""
    all_reviews = {}
    consecutive_empty = 0

    for page in range(1, max_pages + 1):
        tasks[task_id]["message"] = f"正在爬取{'差评' if label_key=='bad' else '好评'} · 第{page}页 · 已获{len(all_reviews)}条"
        html = fetch_review_page(app_id, mapping, page)
        if not html:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
            continue

        page_reviews = parse_page(html)
        if not page_reviews:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
        else:
            consecutive_empty = 0
            for r in page_reviews:
                all_reviews[r["review_id"]] = r

        tasks[task_id][f"{label_key}_count"] = len(all_reviews)
        time.sleep(1.2)  # 礼貌爬取

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
def run_task(task_id, game_name):
    tasks[task_id].update({"status": "running", "step": 0, "error": None, "result": None})
    try:
        # Step 1: 搜索
        tasks[task_id].update({"step": 1, "message": f"正在搜索《{game_name}》..."})
        app_id = search_game(game_name)
        if not app_id:
            raise Exception(f"未找到游戏《{game_name}》，请检查名称是否正确")

        # Step 2: 差评
        tasks[task_id].update({"step": 2, "message": "开始爬取差评..."})
        bad_reviews = scrape_all_reviews(app_id, "差评", task_id, "bad")

        # Step 3: 好评
        tasks[task_id].update({"step": 3, "message": "开始爬取好评..."})
        good_reviews = scrape_all_reviews(app_id, "好评", task_id, "good")

        # Step 4: AI 分析
        tasks[task_id].update({"step": 4, "message": f"DeepSeek 正在分析 {len(bad_reviews)} 条差评 + {len(good_reviews)} 条好评..."})
        analysis = analyze_with_deepseek(bad_reviews, good_reviews, game_name)

        # Step 5: 完成
        tasks[task_id].update({
            "step": 5, "status": "done",
            "message": "分析完成！",
            "result": {
                "game_name": game_name,
                "app_id": app_id,
                "bad_count": len(bad_reviews),
                "good_count": len(good_reviews),
                "analysis": analysis,
            }
        })
    except Exception as e:
        tasks[task_id].update({"status": "error", "error": str(e), "message": str(e)})

# ────────────────────────────────────────
# API 路由
# ────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.json or {}
    game_name = (data.get("game_name") or "").strip()
    if not game_name:
        return jsonify({"error": "请输入游戏名称"}), 400

    import uuid
    task_id = str(uuid.uuid4())
    tasks[task_id] = {
        "status": "idle", "step": 0, "total_steps": 5,
        "message": "", "bad_count": 0, "good_count": 0,
        "result": None, "error": None,
    }
    t = threading.Thread(target=run_task, args=(task_id, game_name))
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
