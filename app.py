from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
from openai import OpenAI
import time, re, json, os, threading

app = Flask(__name__)
CORS(app)

DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# ── 模拟真实浏览器 Headers ──
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.taptap.cn/",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin",
    "Upgrade-Insecure-Requests": "1",
}

API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.taptap.cn/",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://www.taptap.cn",
}

# ── 全局任务状态 ──
tasks = {}

# ── Session（复用 Cookie）──
session = requests.Session()
session.headers.update(HEADERS)

def warm_up_session():
    """访问首页获取 Cookie，模拟真实浏览器"""
    try:
        session.get("https://www.taptap.cn/", timeout=10)
    except Exception:
        pass

# ────────────────────────────────────────
# 搜索游戏，返回 (app_id, game_title)
# ────────────────────────────────────────
def search_game(game_name):
    """
    多策略搜索，依次尝试：
    1. TapTap 内部 JSON API（最稳定）
    2. 带 Session Cookie 的搜索页 HTML 解析
    3. 从游戏详情 URL 直接匹配（若输入的是纯数字 ID）
    """
    # ── 0. 如果用户直接输入了数字 ID ──
    if re.match(r"^\d+$", game_name.strip()):
        return game_name.strip(), f"App#{game_name.strip()}"

    warm_up_session()

    # ── 1. TapTap 搜索 JSON API ──
    # 已知可用的几个 API 端点，逐一尝试
    api_candidates = [
        f"https://www.taptap.cn/api/v2/app/list-by-keyword?kw={requests.utils.quote(game_name)}&limit=10&type=app",
        f"https://www.taptap.cn/api/v2/search/game?q={requests.utils.quote(game_name)}&limit=10",
        f"https://www.taptap.cn/api/v1/search?q={requests.utils.quote(game_name)}&type=game&limit=10",
    ]
    for url in api_candidates:
        try:
            r = session.get(url, headers=API_HEADERS, timeout=12)
            if r.status_code == 200:
                try:
                    data = r.json()
                except Exception:
                    continue
                app_id, title = _extract_id_from_json(data)
                if app_id:
                    print(f"[API] found id={app_id} title={title}")
                    return app_id, title
        except Exception as e:
            print(f"[API] {url} -> {e}")

    # ── 2. 搜索结果页 HTML（带 Cookie）──
    search_url = f"https://www.taptap.cn/search?q={requests.utils.quote(game_name)}&type=app"
    try:
        r = session.get(search_url, timeout=18)
        html = r.text

        # 2a. 尝试解析内嵌 JSON（SSR __INITIAL_STATE__ 或 window.__DATA__）
        for pattern in [
            r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*</script>',
            r'window\.__DATA__\s*=\s*(\{.*?\});\s*</script>',
            r'<script[^>]*>\s*\(function[^)]*\)\s*(\{.*?"appList".*?\})\s*\)',
        ]:
            m = re.search(pattern, html, re.DOTALL)
            if m:
                try:
                    obj = json.loads(m.group(1))
                    app_id, title = _find_app_id_recursive(obj)
                    if app_id:
                        print(f"[SSR-JSON] found id={app_id}")
                        return app_id, title or game_name
                except Exception:
                    pass

        # 2b. 直接找 /app/数字 href
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            m = re.search(r"/app/(\d+)", a["href"])
            if m:
                print(f"[HTML-href] found id={m.group(1)}")
                return m.group(1), game_name

    except Exception as e:
        print(f"[HTML] {e}")

    return None, None


def _extract_id_from_json(data):
    """从各种 TapTap API 响应结构中取 app_id 和 title"""
    # 常见结构路径
    candidates = [
        data.get("data", {}).get("list"),
        data.get("data", {}).get("apps"),
        data.get("data", {}).get("items"),
        data.get("list"),
        data.get("apps"),
        data.get("result", {}).get("hits"),
    ]
    for lst in candidates:
        if isinstance(lst, list) and lst:
            item = lst[0]
            # 可能有嵌套 app 对象
            app_obj = item.get("app") or item
            aid = app_obj.get("id") or app_obj.get("app_id")
            title = app_obj.get("title") or app_obj.get("name") or ""
            if aid:
                return str(aid), title
    return None, None


def _find_app_id_recursive(obj, depth=0):
    """递归从嵌套 JSON 找 app id（>10000 的数字）"""
    if depth > 8:
        return None, None
    if isinstance(obj, dict):
        # 优先匹配 {id: ..., title: ...} 且 id 是较大数字的 dict
        aid = obj.get("id") or obj.get("app_id")
        title = obj.get("title") or obj.get("name") or ""
        if aid and str(aid).isdigit() and int(str(aid)) > 10000:
            return str(aid), title
        for v in obj.values():
            r, t = _find_app_id_recursive(v, depth + 1)
            if r:
                return r, t
    elif isinstance(obj, list):
        for item in obj[:10]:
            r, t = _find_app_id_recursive(item, depth + 1)
            if r:
                return r, t
    return None, None


# ────────────────────────────────────────
# 爬取评论：优先用 JSON API，降级到 HTML 解析
# ────────────────────────────────────────
def fetch_reviews_api(app_id, label, page, page_size=10):
    """
    TapTap 评论 JSON API。
    label: 1=好评, 2=差评（也有用 "good"/"bad" 的版本）
    """
    # 已知可用的 API 端点
    apis = [
        {
            "url": f"https://www.taptap.cn/api/v2/review/v2/by-app",
            "params": {
                "app_id": app_id,
                "label": label,   # 1=好评 2=差评
                "page": page,
                "limit": page_size,
                "order": "default",
            }
        },
        {
            "url": f"https://www.taptap.cn/api/v2/review/by-app",
            "params": {
                "app_id": app_id,
                "label": label,
                "page": page,
                "limit": page_size,
            }
        },
    ]
    for api in apis:
        try:
            r = session.get(api["url"], headers=API_HEADERS, params=api["params"], timeout=15)
            if r.status_code == 200:
                data = r.json()
                items = (
                    data.get("data", {}).get("list")
                    or data.get("data", {}).get("items")
                    or data.get("list")
                    or []
                )
                if isinstance(items, list):
                    return items
        except Exception as e:
            print(f"[Review API] {e}")
    return None  # None 表示 API 全部失败，触发 HTML 降级


def parse_review_item(item):
    """从 JSON item 解析评论字段"""
    # 评论内容可能在多个字段
    content = (
        item.get("contents", {}).get("summary", {}).get("text")
        or item.get("comment", {}).get("contents", {}).get("summary", {}).get("text")
        or item.get("body")
        or item.get("content")
        or ""
    )
    rid = str(item.get("id") or item.get("review_id") or "")
    author = (
        item.get("author", {}).get("name")
        or item.get("user", {}).get("name")
        or ""
    )
    pub_time = str(item.get("created_time") or item.get("publish_time") or "")
    return {
        "review_id": rid,
        "author": author,
        "content": content,
        "time": pub_time,
        "device": "",
        "url": f"https://www.taptap.cn/review/{rid}" if rid else "",
    }


def fetch_review_page_html(app_id, mapping, page=1):
    """HTML 降级爬取"""
    url = f"https://www.taptap.cn/app/{app_id}/review"
    params = {"os": "android", "mapping": mapping, "label": "0", "page": page}
    try:
        r = session.get(url, params=params, timeout=20)
        return r.text
    except Exception as e:
        print(f"[HTML Review] page={page}: {e}")
        return ""


def parse_page_html(html):
    """从 HTML 中提取评论"""
    soup = BeautifulSoup(html, "html.parser")
    reviews = []
    seen = set()
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
        author, pub_time, device = "", "", ""
        container = link.find_parent("div")
        if container:
            user_link = container.find("a", href=re.compile(r"/user/\d+"))
            if user_link:
                author = user_link.get_text(strip=True)
            t = container.find(string=re.compile(r"\d{4}/\d{1,2}/\d{1,2}"))
            if t:
                pub_time = str(t).strip()
            d = container.find(string=re.compile(r"来自"))
            if d:
                device = str(d).strip()
        reviews.append({
            "review_id": rid, "author": author,
            "content": content, "time": pub_time,
            "device": device,
            "url": "https://www.taptap.cn" + href,
        })
    return reviews


def scrape_all_reviews(app_id, label_num, html_mapping, task_id, label_key, max_pages=50):
    """
    翻页爬取所有评论。
    先试 JSON API（label_num），失败降级到 HTML（html_mapping）。
    """
    all_reviews = {}
    consecutive_empty = 0
    use_api = True  # 先用 JSON API

    for page in range(1, max_pages + 1):
        mode = "API" if use_api else "HTML"
        tasks[task_id]["message"] = (
            f"正在爬取{'差评' if label_key=='bad' else '好评'} [{mode}] · "
            f"第{page}页 · 已获{len(all_reviews)}条"
        )

        if use_api:
            items = fetch_reviews_api(app_id, label_num, page)
            if items is None:
                # API 全部失败，切换到 HTML
                print(f"[scrape] API 失败，切换 HTML 模式")
                use_api = False
                items = []
            if use_api and not items:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    break
                time.sleep(0.8)
                continue
            if use_api:
                consecutive_empty = 0
                for item in items:
                    r = parse_review_item(item)
                    if r["review_id"] and r["content"] and len(r["content"]) >= 3:
                        all_reviews[r["review_id"]] = r
                tasks[task_id][f"{label_key}_count"] = len(all_reviews)
                time.sleep(0.8)
                continue

        # HTML 模式
        html = fetch_review_page_html(app_id, html_mapping, page)
        page_reviews = parse_page_html(html) if html else []
        if not page_reviews:
            consecutive_empty += 1
            if consecutive_empty >= 3:
                break
        else:
            consecutive_empty = 0
            for r in page_reviews:
                all_reviews[r["review_id"]] = r
        tasks[task_id][f"{label_key}_count"] = len(all_reviews)
        time.sleep(1.2)

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
    try:
        if app_id_override:
            app_id = app_id_override
            resolved_name = game_name or f"App#{app_id}"
            tasks[task_id].update({"step": 1, "message": f"使用 App ID={app_id}，跳过搜索..."})
        else:
            tasks[task_id].update({"step": 1, "message": f"正在搜索《{game_name}》..."})
            app_id, resolved_name = search_game(game_name)
            if not app_id:
                raise Exception(
                    f"未找到游戏《{game_name}》。"
                    "请尝试：① 换个关键词 ② 在 TapTap 找到游戏页面，复制网址中的数字ID后重新输入"
                )
            resolved_name = resolved_name or game_name

        # Step 2: 差评（label=2）
        tasks[task_id].update({"step": 2, "message": "开始爬取差评..."})
        bad_reviews = scrape_all_reviews(app_id, 2, "差评", task_id, "bad")

        # Step 3: 好评（label=1）
        tasks[task_id].update({"step": 3, "message": "开始爬取好评..."})
        good_reviews = scrape_all_reviews(app_id, 1, "好评", task_id, "good")

        if not bad_reviews and not good_reviews:
            raise Exception(
                f"未能爬取到任何评论（App ID={app_id}）。"
                "可能原因：① 该游戏评论较少 ② TapTap 限流，请稍后重试"
            )

        # Step 4: AI 分析
        tasks[task_id].update({"step": 4, "message": f"DeepSeek 正在分析 {len(bad_reviews)} 条差评 + {len(good_reviews)} 条好评..."})
        analysis = analyze_with_deepseek(bad_reviews, good_reviews, resolved_name)

        # Step 5: 完成
        tasks[task_id].update({
            "step": 5, "status": "done",
            "message": "分析完成！",
            "result": {
                "game_name": resolved_name,
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
    app_id_override = (data.get("app_id") or "").strip()

    if not game_name and not app_id_override:
        return jsonify({"error": "请输入游戏名称或 App ID"}), 400

    import uuid
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
