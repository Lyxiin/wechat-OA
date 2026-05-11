# wechat-OA

这是一个微信公众号文章采集、清洗、股票信息提取和网页展示的小工具。

目前项目主要围绕盘前类公众号文章做处理：先采集公众号文章正文，再保存到 SQLite 数据库，然后调用 GPT API 提取文章里提到的股票、理由和利好/利空倾向，最后通过网页进行筛选和统计。

## 已有功能

- 按公众号名称和时间范围采集文章
- 提取文章详情页正文，而不是只保存链接
- 清洗网页内容，只保留有用正文
- 生成 JSONL 数据文件
- 导入 SQLite 数据库
- 调用 GPT API 提取股票信息
- 网页端按日期、公众号、股票进行筛选
- 支持股票视角统计，例如过去 7/14/30 天提及次数
- 支持账号密码登录
- 管理员可以创建普通用户

## 主要文件

- `wechat_crawler.py`：采集公众号文章并清洗正文
- `wechat_db.py`：把 JSONL 文章数据导入 SQLite
- `stock_extractor.py`：调用 GPT API 提取文章里的股票信息
- `stock_web.py`：启动网页服务和后端接口
- `web/index.html`：前端页面
- `.env.example`：环境变量示例
- `requirements.txt`：Python 依赖
- `run_stock_web.bat`：Windows 下一键启动网页服务

## 环境准备

```powershell
python -m pip install -r requirements.txt
```

复制环境变量示例：

```powershell
copy .env.example .env
```

然后编辑 `.env`，填入自己的 GPT API Key：

```env
LLM_API_URL=https://api.ikuncode.cc/v1/chat/completions
LLM_API_KEY=replace_with_your_key
LLM_MODEL=gpt-5.4-mini
```

`.env` 不会上传到 GitHub，避免泄露密钥。

## 常用命令

采集公众号文章：

```powershell
python wechat_crawler.py "盘前纪要" 20260101 20260601 --output ./wechat_articles
```

导入数据库：

```powershell
python wechat_db.py import ./wechat_articles/盘前纪要/articles.jsonl
```

提取股票信息：

```powershell
python stock_extractor.py run
```

启动网页：

```powershell
python stock_web.py --host 127.0.0.1 --port 8088
```

浏览器打开：

```text
http://127.0.0.1:8088
```

第一次启动时，如果数据库里还没有账号，系统会自动创建管理员账号 `admin`。如果没有提前设置 `STOCK_WEB_ADMIN_PASSWORD`，启动日志里会打印一个随机生成的管理员密码。

## 数据说明

默认数据库位置：

```text
wechat_articles/articles.sqlite
```

数据库、采集结果、日志和 `.env` 都已通过 `.gitignore` 排除，不会上传到 GitHub。仓库里只保留代码、页面、测试和示例配置。
