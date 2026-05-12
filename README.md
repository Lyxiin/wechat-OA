# wechat-OA

这是一个微信公众号文章采集、清洗、股票信息提取和网页展示的小工具。

项目现在主要围绕盘前类公众号文章做处理：先采集公众号文章正文，再保存到 SQLite 数据库，然后调用 GPT API 提取文章里提到的股票、理由和利好/利空倾向，最后通过网页进行筛选和统计。

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
- 支持一条命令跑完整自动化链路

## 主要文件

- `config/accounts.json`：公众号自动采集配置
- `auto_pipeline.py`：自动化总控脚本
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

## 自动化采集

公众号列表放在：

```text
config/accounts.json
```

默认已经配置了：

```text
盘前纪要
盘前早咖
```

要新增公众号，只需要在 `accounts` 里加一段：

```json
{
  "name": "新的公众号名",
  "enabled": true,
  "crawl_days": 7,
  "limit": 1000,
  "seed_urls": []
}
```

初始化自动化状态表：

```powershell
python auto_pipeline.py init-db
```

跑完整链路：

```powershell
python auto_pipeline.py run
```

只跑某一个公众号：

```powershell
python auto_pipeline.py run --account "盘前纪要"
```

临时补采最近 30 天：

```powershell
python auto_pipeline.py run --account "盘前纪要" --days 30
```

强制重新跑 GPT 股票提取：

```powershell
python auto_pipeline.py run --account "盘前纪要" --force
```

查看最近一次自动化运行状态：

```powershell
python auto_pipeline.py status
```

自动化脚本会做这几件事：

```text
读取公众号配置
获取最近 N 天文章
清洗并写入 articles.jsonl
导入 SQLite
只挑选未提取或内容变更的文章调用 GPT
记录 pipeline_runs 和 crawl_state 状态
```

如果某个公众号失败，脚本会记录失败原因，并继续处理其他公众号。

## 手动命令

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

第一次启动网页时，如果数据库里还没有账号，系统会自动创建管理员账号 `admin`。如果没有提前设置 `STOCK_WEB_ADMIN_PASSWORD`，启动日志里会打印一个随机生成的管理员密码。

## 服务器定时任务参考

阿里云 Linux 上可以用 `cron` 定时跑自动链路。

编辑定时任务：

```bash
crontab -e
```

工作日早上 7 点到 10 点，每 30 分钟跑一次：

```cron
*/30 7-10 * * 1-5 cd /opt/wechat-OA/wechat-OA && python3 auto_pipeline.py run >> logs/pipeline.log 2>&1
```

注意：如果 WeWe RSS 的微信登录过期，需要打开 WeWe RSS 页面重新扫码授权。

## 数据说明

默认数据库位置：

```text
wechat_articles/articles.sqlite
```

主要数据表：

```text
articles            文章正文
stock_mentions      股票提取结果
extraction_runs     GPT 提取运行记录
pipeline_runs       自动化总任务记录
crawl_state         每个公众号的采集状态
```

数据库、采集结果、日志和 `.env` 都已通过 `.gitignore` 排除，不会上传到 GitHub。仓库里只保留代码、页面、测试和示例配置。
