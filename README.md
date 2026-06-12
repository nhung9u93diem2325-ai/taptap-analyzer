# TapTap 游戏评论分析工具

输入游戏名称，自动爬取 TapTap 全量差评与好评，由 DeepSeek AI 归纳分析。

## 本地运行

```bash
pip install -r requirements.txt
set DEEPSEEK_API_KEY=你的Key     # Windows
# export DEEPSEEK_API_KEY=你的Key  # Mac/Linux
python app.py
```
浏览器打开 http://127.0.0.1:5000

## 部署到 Render（免费）

1. 将本项目推送到 GitHub
2. 登录 https://render.com → New → Web Service → 连接仓库
3. 配置：
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --timeout 600`
4. Environment Variables 添加：`DEEPSEEK_API_KEY` = 你的Key
5. 部署完成后，复制 Render 给的网址（如 https://taptap-analyzer.onrender.com）
6. 打开 templates/index.html，找到这行，改成你的 Render 地址：
   ```
   : "https://taptap-analyzer.onrender.com";
   ```
7. 重新推送 GitHub，Render 自动重新部署

## 文件结构

```
taptap-analyzer/
├── app.py              # 后端（Flask + 爬虫 + DeepSeek）
├── templates/
│   └── index.html      # 前端页面
├── requirements.txt    # 依赖
├── Procfile            # Render 启动命令
└── README.md
```
