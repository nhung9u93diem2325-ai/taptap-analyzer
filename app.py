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
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.taptap.cn/",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Accept": "application/json, text/plain, */*",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": "https://www.taptap.cn",
}

tasks = {}
session = requests.Session()
session.headers.update(HEADERS)

def warm_up_session():
    try:
        session.get("https://www.taptap.cn/", timeout=10)
    except Exception:
        pass

# ────────────────────────────────────────
# 搜索游戏
# ────────────────────────────────────────
def search_game(game_name):
    if re.match(r"^\d+$", game_name.strip()):
        return game_name.strip(), f"App#{game_name.strip()}"

    warm_up_session()

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
                    return app_id, title
        except Exception as e:
            print(f"[API search] {url} -> {e}")

    search_url = f"https://www.taptap.cn/search?q={requests.utils.quote(game_name)}&type=app"
    try:
        r = session.get(search_url, timeout=18)
        html = r.text
        for pattern in [
            r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*</script>',
            r'window\.__DATA__\s*=\s*(\{.*?\});\s*</script>',
        ]:
            m = re.search(pattern, html, re.DOTALL)
            if m:
                try:
                    obj = json.loads(m.group(1))
                    app_id, title = _find_app_id_recursive(obj)
                    if app_id:
                        return app_id, title or game_name
                except Exception:
                    pass
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            m = re.search(r"/app/(\d+)", a["href"])
            if m:
                return m.group(1), game_name
    except Exception as e:
        print(f"[HTML search] {e}")

    return None, None


def _extract_id_from_json(data):
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
            app_obj = item.get("app") or item
            aid = app_obj.get("id") or app_obj.get("app_id")
            title = app_obj.get("title") or app_obj.get("name") or ""
            if aid:
                return str(aid), title
    return None, None


def _find_app_id_recursive(obj, depth=0):
    if depth > 8:
        return None, None
    if isinstance(obj, dict):
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
# 评论爬取（核心重写）
# ────────────────────────────────────────

def _try_review_api(app_id, label_str, page, page_size=20):
    """
    尝试所有已知的 TapTap 评论 JSON API 格式。
    label_str: "bad" 或 "good"
    返回 (items_list, api_worked)
      items_list: 评论列表（可能为空）
      api_worked: True 表示 API 通了（即使返回空），False 表示全部失败
    """
    # TapTap 不同版本 API 的 label 参数格式各不相同，全部枚举
    label_variants = {
        "bad":  [2, "2", "bad",  "negative", 0],
        "good": [1, "1", "good", "positive", 5],
    }
    labels = label_variants[label_str]

    # 已知端点（按成功率排序）
    endpoints = [
        "https://www.taptap.cn/api/v2/review/v2/by-app",
        "https://www.taptap.cn/api/v2/review/by-app",
        "https://www.taptap.cn/api/v1/review/by-app",
    ]

    for endpoint in endpoints:
        for lv in labels:
            params_variants = [
                # 变体1：label 作为独立参数
                {"app_id": app_id, "label": lv, "page": page, "limit": page_size, "order": "default"},
                # 变体2：用 score 参数区分好差评
                {"app_id": app_id, "score": lv, "page": page, "limit": page_size},
                # 变体3：offset 分页
                {"app_id": app_id, "label": lv, "offset": (page - 1) * page_size, "limit": page_size},
            ]
            for params in params_variants:
                try:
                    r = session.get(endpoint, headers=API_HEADERS, params=params, timeout=15)
                    if r.status_code != 200:
                        continue
                    try:
                        data = r.json()
                    except Exception:
                        continue

                    # 检查响应是否有效（code=0 或 success=true 之类）
                    code = data.get("code") or data.get("status") or 0
                    if str(code) not in ("0", "200", "success", "0"):
                        # 有些API用 code!=0 表示错误
                        if data.get("error") or data.get("message", "").lower() in ("error", "fail"):
                            continue

                    items = (
                        data.get("data", {}).get("list")
                        or data.get("data", {}).get("items")
                        or data.get("data", {}).get("review_list")
                        or data.get("list")
                        or data.get("items")
                        or []
                    )
                    if not isinstance(items, list):
                        continue

                    print(f"[ReviewAPI✓] endpoint={endpoint} label={lv} page={page} -> {len(items)} items")
                    return items, True  # API 通了

                except Exception as e:
                    print(f"[ReviewAPI] {endpoint} label={lv}: {e}")
                    continue

    return [], False  # 全部失败


def parse_review_item(item):
    """从 JSON 响应解析单条评论，兼容多种字段结构"""
    # 内容字段（各版本 API 字段名不同）
    content = ""
    for path in [
        lambda x: x.get("contents", {}).get("summary", {}).get("text", ""),
        lambda x: x.get("comment", {}).get("contents", {}).get("summary", {}).get("text", ""),
        lambda x: x.get("review", {}).get("contents", {}).get("summary", {}).get("text", ""),
        lambda x: x.get("body", ""),
        lambda x: x.get("content", ""),
        lambda x: x.get("text", ""),
        lambda x: x.get("summary", ""),
    ]:
        try:
            val = path(item)
            if val and len(str(val)) >= 3:
                content = str(val)
                break
        except Exception:
            pass

    rid = str(
        item.get("id") or item.get("review_id")
        or item.get("comment_id") or item.get("review", {}).get("id") or ""
    )
    author = (
        item.get("author", {}).get("name")
        or item.get("user", {}).get("name")
        or item.get("reviewer", {}).get("name")
        or ""
    )
    pub_time = str(item.get("created_time") or item.get("publish_time") or item.get("created_at") or "")

    return {
        "review_id": rid or str(hash(content)),
        "author": author,
        "content": content,
        "time": pub_time,
        "device": "",
        "url": f"https://www.taptap.cn/review/{rid}" if rid else "",
    }


def fetch_review_page_html(app_id, score_filter, page=1):
    """
    HTML 降级爬取评论页。
    score_filter: "bad"(差评) 或 "good"(好评)
    TapTap 评论页 URL: /app/{id}/review?score=1,2 (差评) 或 score=4,5 (好评)
    也尝试 /review 默认列表页从中筛选
    """
    # 尝试多种 URL 参数格式
    url_variants = []

    if score_filter == "bad":
        # 差评：1-2星
        url_variants = [
            (f"https://www.taptap.cn/app/{app_id}/review", {"score": "1,2", "page": page}),
            (f"https://www.taptap.cn/app/{app_id}/review", {"label": "2", "page": page}),
            (f"https://www.taptap.cn/app/{app_id}/review", {"mapping": "2", "page": page}),
            (f"https://www.taptap.cn/app/{app_id}/review", {"os": "android", "mapping": "bad", "page": page}),
        ]
    else:
        # 好评：4-5星
        url_variants = [
            (f"https://www.taptap.cn/app/{app_id}/review", {"score": "4,5", "page": page}),
            (f"https://www.taptap.cn/app/{app_id}/review", {"label": "1", "page": page}),
            (f"https://www.taptap.cn/app/{app_id}/review", {"mapping": "1", "page": page}),
            (f"https://www.taptap.cn/app/{app_id}/review", {"os": "android", "mapping": "good", "page": page}),
        ]

    for url, params in url_variants:
        try:
            r = session.get(url, params=params, timeout=20)
            html = r.text
            reviews = parse_page_html(html, score_filter)
            if reviews:
                print(f"[HTML✓] url={url} params={params} -> {len(reviews)} reviews")
                return reviews
        except Exception as e:
            print(f"[HTML] {url} {params}: {e}")

    return []


def parse_page_html(html, score_filter=None):
    """从 HTML 中提取评论"""
    soup = BeautifulSoup(html, "html.parser")
    reviews = []
    seen = set()

    # 尝试从内嵌 JSON 提取（SSR 数据）
    for pattern in [
        r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});\s*</script>',
        r'"reviewList"\s*:\s*(\[.*?\])',
        r'"review_list"\s*:\s*(\[.*?\])',
    ]:
        m = re.search(pattern, html, re.DOTALL)
        if m:
            try:
                obj = json.loads(m.group(1))
                lst = obj if isinstance(obj, list) else _find_review_list(obj)
                if lst:
                    for item in lst:
                        r = parse_review_item(item)
                        if r["content"] and r["review_id"] not in seen:
                            seen.add(r["review_id"])
                            reviews.append(r)
                    if reviews:
                        return reviews
            except Exception:
                pass

    # 常规 HTML 链接解析
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
        author, pub_time = "", ""
        container = link.find_parent("div")
        if container:
            user_link = container.find("a", href=re.compile(r"/user/\d+"))
            if user_link:
                author = user_link.get_text(strip=True)
            t = container.find(string=re.compile(r"\d{4}/\d{1,2}/\d{1,2}"))
            if t:
                pub_time = str(t).strip()
        reviews.append({
            "review_id": rid, "author": author,
            "content": content, "time": pub_time, "device": "",
            "url": "https://www.taptap.cn" + href,
        })
    return reviews


def _find_review_list(obj, depth=0):
    if depth > 6:
        return None
    if isinstance(obj, dict):
        for key in ("reviewList", "review_list", "list", "items", "reviews"):
            if key in obj and isinstance(obj[key], list) and obj[key]:
                return obj[key]
        for v in obj.values():
            r = _find_review_list(v, depth + 1)
            if r:
                return r
    return None


def scrape_all_reviews(app_id, label_str, task_id, label_key, max_pages=50):
    """
    翻页爬取所有评论。
    label_str: "bad" 或 "good"
    策略：先探测 API 是否可用（第1页），可用则全程 API，否则全程 HTML。
    """
    all_reviews = {}
    consecutive_empty = 0

    # ── 第0步：探测 API 是否可用 ──
    tasks[task_id]["message"] = f"正在探测{'差评' if label_key=='bad' else '好评'}接口..."
    probe_items, api_ok = _try_review_api(app_id, label_str, 1)
    use_api = api_ok

    if use_api:
        print(f"[scrape] 使用 JSON API 模式 label={label_str}")
        # 处理第1页探测结果
        for item in probe_items:
            r = parse_review_item(item)
            if r["content"] and len(r["content"]) >= 3:
                all_reviews[r["review_id"]] = r
        tasks[task_id][f"{label_key}_count"] = len(all_reviews)

        if not probe_items:
            consecutive_empty = 1

        # 继续翻页 2..max_pages
        for page in range(2, max_pages + 1):
            tasks[task_id]["message"] = (
                f"正在爬取{'差评' if label_key=='bad' else '好评'} [API] · "
                f"第{page}页 · 已获{len(all_reviews)}条"
            )
            items, _ = _try_review_api(app_id, label_str, page)
            if not items:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    break
            else:
                consecutive_empty = 0
                for item in items:
                    r = parse_review_item(item)
                    if r["content"] and len(r["content"]) >= 3:
                        all_reviews[r["review_id"]] = r
            tasks[task_id][f"{label_key}_count"] = len(all_reviews)
            time.sleep(0.8)

    else:
        # ── HTML 降级模式 ──
        print(f"[scrape] API 不可用，使用 HTML 模式 label={label_str}")
        for page in range(1, max_pages + 1):
            tasks[task_id]["message"] = (
                f"正在爬取{'差评' if label_key=='bad' else '好评'} [HTML] · "
                f"第{page}页 · 已获{len(all_reviews)}条"
            )
            page_reviews = fetch_review_page_html(app_id, label_str, page)
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
                    "请尝试：① 换个关键词 ② 在 TapTap 找到游戏页面，复制网址中的数字ID后填入下方输入框"
                )
            resolved_name = resolved_name or game_name

        # Step 2: 差评
        tasks[task_id].update({"step": 2, "message": "开始爬取差评..."})
        bad_reviews = scrape_all_reviews(app_id, "bad", task_id, "bad")

        # Step 3: 好评
        tasks[task_id].update({"step": 3, "message": "开始爬取好评..."})
        good_reviews = scrape_all_reviews(app_id, "good", task_id, "good")

        if not bad_reviews and not good_reviews:
            raise Exception(
                f"未能爬取到任何评论（App ID={app_id}）。\n"
                "TapTap 可能临时限流，建议等待 2-3 分钟后重试。\n"
                "如果持续失败，请在服务器控制台查看详细日志。"
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
        tasks[task_id].update({"status": "error", "error": str(e), "message": str(e)})


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

# ── 新增：调试接口，查看某 app 评论 API 返回内容 ──
@app.route("/debug/review/<app_id>")
def debug_review(app_id):
    """访问 /debug/review/746164 可以直接看 API 原始返回，方便排查"""
    results = {}
    endpoints = [
        "https://www.taptap.cn/api/v2/review/v2/by-app",
        "https://www.taptap.cn/api/v2/review/by-app",
    ]
    for ep in endpoints:
        for label in [1, 2]:
            key = f"{ep}?label={label}"
            try:
                r = session.get(ep, headers=API_HEADERS,
                                params={"app_id": app_id, "label": label, "page": 1, "limit": 5},
                                timeout=10)
                results[key] = {"status": r.status_code, "body": r.text[:500]}
            except Exception as e:
                results[key] = {"error": str(e)}
    return jsonify(results)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"启动成功！请用浏览器打开：http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
