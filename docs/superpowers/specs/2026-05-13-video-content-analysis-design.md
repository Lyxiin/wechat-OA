# 公开视频内容分析练习项目设计

日期：2026-05-13

## 目标

构建一个命令行练习项目，用于对公开分享的视频做学习和研究用途的内容分析。第一版从用户提供的公开视频链接中提取音频，进行语音识别，生成通用的结构化内容分析，并同时保存人类可读的文件产物和 SQLite 索引记录。

项目只使用用户已经提供的链接和 cookies。项目不得尝试绕过隐私设置、身份认证、付费墙或平台访问控制。

## 已确认决策

- 输入方式：同时支持直接视频/分享链接和未来的账号批量采集，其中直接 URL 列表作为第一版稳定工作流。
- 分析方式：使用通用可扩展结构，包含摘要、主题、关键词、时间线、关键要点、实体、开放问题，以及用于未来领域分析的扩展字段。
- 持久化方式：写入文件产物以便检查，同时保存 SQLite 记录用于索引、状态跟踪，以及后续搜索或看板。
- 模型接入：使用适配器边界。默认适配器复用现有 Groq Whisper ASR 客户端和现有 OpenAI-style LLM 配置，同时为以后切换本地模型或其他服务留出空间。
- 用户界面：命令行优先。等管道可靠并有测试覆盖后，再考虑增加网页查看器。

## 推荐方案

新增一个独立的 `video_analysis.py` 管道，复用现有 ASR 和 Douyin/yt-dlp 能力，但不把这个学习项目耦合到现有股票文章流水线里。

相比直接扩展 `douyin_pipeline.py`，这个方案能让账号抓取、财经提取和通用视频分析保持分离。相比搭建完整插件式媒体框架，这个方案也更适合第一版，因为第一版应优先跑通可工作的学习闭环。

## 命令行接口

第一版应提供：

```powershell
python video_analysis.py init-db
python video_analysis.py run --urls urls.txt
python video_analysis.py run --url "https://..."
```

可选的重跑控制：

```powershell
python video_analysis.py run --urls urls.txt --force-download
python video_analysis.py run --urls urls.txt --force-transcribe
python video_analysis.py run --urls urls.txt --force-analyze
```

预期默认路径：

- 输入 cookies：`cookies/cookies.txt`，除非被配置或环境变量覆盖。
- 输出目录：`video_analysis_data/`。
- SQLite 数据库：`video_analysis_data/video_analysis.sqlite`。

## 架构

管道应拆成接口清晰的小单元：

- `VideoSourceReader`：读取一个 `--url` 值或一个 `--urls` 文本文件，规范化空白，忽略空行/注释行，并在保持顺序的同时去重链接。
- `VideoDownloader`：通过 `yt-dlp` 解析视频元数据，下载或提取音频，并写入元数据和音频文件。它应复用 `douyin_crawler.py` 中已有的 cookies 处理模式。
- `ASRAdapter`：转写音频。默认实现包装 `asr_client.GroqASRClient`。
- `ContentAnalyzer`：把 transcript 文本和元数据发送给 OpenAI-style LLM，并校验返回的 JSON 分析结果。
- `VideoAnalysisStore`：写入 SQLite 行和文件产物，跟踪状态，并支持缓存判断。
- `PipelineRunner`：编排单个视频的执行流程，并隔离失败。

## 数据流

对每个输入链接：

1. 规范化 URL 并去重。
2. 通过 `yt-dlp` 解析元数据和 canonical URL。
3. 在 `video_analysis_data/<video_id>/` 下创建稳定输出目录。
4. 下载或复用音频。
5. 转写或复用 transcript。
6. 当 transcript hash 匹配时复用 analysis，否则重新分析 transcript。
7. 写入 `metadata.json`、`transcript.txt`、`analysis.json` 和 `summary.md`。
8. 用状态、元数据、hash、输出路径和错误详情 upsert 一条 SQLite 记录。
9. 即使某个视频失败，也继续处理下一个视频。

## 文件产物

每个视频目录应包含：

- `metadata.json`：标题、作者、来源 URL、canonical URL、发布时间、时长、缩略图 URL，以及选取后的原始元数据。
- `audio.mp3`：可用时保存提取出的音频。
- `transcript.txt`：纯文本转写结果。
- `analysis.json`：已校验的结构化分析。
- `summary.md`：用于快速查看的人类可读结果。

## 分析 JSON

分析器应生成这个稳定结构：

```json
{
  "summary": "Short overall summary.",
  "topics": ["topic"],
  "keywords": ["keyword"],
  "timeline": [
    {"time": "00:00", "event": "What happens or is discussed."}
  ],
  "key_points": ["important point"],
  "entities": ["person, organization, product, place, or concept"],
  "action_items": [],
  "open_questions": [],
  "extensions": {}
}
```

实现应校验顶层对象存在、必需列表字段确实是列表，并且 `summary` 是字符串。如果模型返回无效 JSON，管道应保存原始响应用于调试，并只将当前视频标记为失败。

## SQLite 存储

使用一张轻量表，例如 `video_items`：

- `id`
- `video_id`
- `source_url`
- `canonical_url`
- `title`
- `author`
- `publish_time`
- `duration`
- `thumbnail_url`
- `status`
- `error`
- `transcript_hash`
- `analysis_hash`
- `metadata_path`
- `audio_path`
- `transcript_path`
- `analysis_path`
- `summary_path`
- `created_at`
- `updated_at`

数据库保存可索引的元数据和路径。完整 transcript 和完整 analysis JSON 保留在文件中。

## 缓存

默认行为应避免重复网络请求和模型调用：

- 如果预期音频文件存在且大小非零，则复用音频。
- 如果 `transcript.txt` 存在且非空，则复用 transcript。
- 如果 `analysis.json` 存在，且保存的 transcript hash 与当前 transcript hash 匹配，则复用 analysis。

重跑参数只覆盖对应阶段：

- `--force-download` 重新下载音频。
- `--force-transcribe` 从已有或新下载的音频重新运行 ASR。
- `--force-analyze` 基于当前 transcript 重新运行 LLM 分析。

## 错误处理

失败应按视频隔离。某个 URL 失败不应中断整个批次。

常见错误应提供可读信息：

- 平台需要 cookies 但 cookies 文件缺失。
- `yt-dlp` 未安装或无法运行。
- 下载或元数据解析失败。
- 音频文件未创建。
- ASR API key 缺失或仍是占位值。
- ASR 响应为空。
- LLM API key 缺失或仍是占位值。
- LLM 响应不是有效 JSON。

每个失败项都应更新 SQLite，设置 `status = failed`，保存错误字符串，并继续处理剩余链接。

## 测试策略

默认测试应避免真实网络和真实模型调用。使用 fake downloader、fake ASR client 和 fake analyzer。

覆盖范围应包括：

- URL 文件解析、注释跳过和去重。
- 输出路径生成和稳定的视频目录命名。
- 音频、transcript 和 analysis 缓存行为。
- 使用 fake client 的端到端管道编排。
- 某个 URL 失败时的失败隔离。
- JSON 分析结果校验和无效响应处理。
- SQLite upsert 行为。

单元测试通过后，再用手动验证覆盖真实 `yt-dlp`、cookies、Groq ASR 和 LLM 调用。

## 验收标准

第一版在满足以下条件时完成：

- `python video_analysis.py init-db` 能创建 SQLite schema。
- `python video_analysis.py run --url "<public video url>"` 能创建文件产物和数据库记录。
- `python video_analysis.py run --urls urls.txt` 能处理多个链接，并报告每个视频的结果。
- 已存在的音频、transcript 和 analysis 产物会被复用，除非提供 force 参数。
- fake-client 测试套件无需网络或 API 凭据即可通过。
- 真实 API 凭据不进入 git，通过 `.env` 或环境变量加载。

## 第一版不包含

- 浏览器 UI。
- 搜索看板。
- 超出当前 `yt-dlp` 流程自然支持范围的跨平台媒体抽象。
- 自动账号抓取，但保留与现有 Douyin 账号管道兼容的空间。
- 除通用 `extensions` 字段外的特定领域财经、舆情、课程笔记或知识库提取。
