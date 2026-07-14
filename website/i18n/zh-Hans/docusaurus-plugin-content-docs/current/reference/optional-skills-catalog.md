---
sidebar_position: 9
title: "可选技能目录"
description: "hermes-agent 附带的官方可选技能 — 通过 hermes skills install official/<category>/<skill> 安装"
---

# 可选技能目录

可选技能随 hermes-agent 一起发布，位于 `optional-skills/` 目录下，但**默认未激活**。请显式安装：

```bash
hermes skills install official/<category>/<skill>
```

示例：

```bash
hermes skills install official/blockchain/solana
hermes skills install official/security/1password
```

下方每个技能均链接至专属页面，包含完整定义、配置和使用说明。

卸载方式：

```bash
hermes skills uninstall <skill-name>
```

## autonomous-ai-agents

| 技能 | 描述 |
|-------|-------------|
| [**blackbox**](/user-guide/skills/optional/autonomous-ai-agents/autonomous-ai-agents-blackbox) | 将编码任务委托给 Blackbox AI CLI agent。内置评判机制的多模型 agent，通过多个 LLM 运行任务并选出最佳结果。需要 blackbox CLI 和 Blackbox AI API 密钥。 |
| [**honcho**](/user-guide/skills/optional/autonomous-ai-agents/autonomous-ai-agents-honcho) | 配置并使用 Honcho 记忆与 Hermes — 跨会话用户建模、多配置文件对等隔离、观测配置、辩证推理、会话摘要及上下文预算执行。适用于配置 Honcho、故障排查等场景。 |

## blockchain

| 技能 | 描述 |
|-------|-------------|
| [**evm**](/user-guide/skills/optional/blockchain/blockchain-evm) | 只读 EVM 客户端：支持 8 条链的钱包、代币、Gas 查询。 |
| [**hyperliquid**](/user-guide/skills/optional/blockchain/blockchain-hyperliquid) | Hyperliquid 市场数据、账户历史、交易回顾。 |
| [**solana**](/user-guide/skills/optional/blockchain/blockchain-solana) | 查询 Solana 链上数据并附带 USD 定价 — 钱包余额、带估值的代币组合、交易详情、NFT、巨鲸检测及实时网络统计。使用 Solana RPC + CoinGecko，无需 API 密钥。 |

## communication

| 技能 | 描述 |
|-------|-------------|
| [**one-three-one-rule**](/user-guide/skills/optional/communication/communication-one-three-one-rule) | 用于技术提案和权衡分析的结构化决策框架。当用户面临多种方案选择（架构决策、工具选型、重构策略、迁移路径）时，本技能提供系统化的分析流程。 |

## creative

| 技能 | 描述 |
|-------|-------------|
| [**blender-mcp**](/user-guide/skills/optional/creative/creative-blender-mcp) | 通过 socket 连接 blender-mcp 插件，直接从 Hermes 控制 Blender。创建 3D 对象、材质、动画，并运行任意 Blender Python（bpy）代码。适用于用户希望在 Blender 中创建或修改任何内容的场景。 |
| [**concept-diagrams**](/user-guide/skills/optional/creative/creative-concept-diagrams) | 生成扁平、极简、支持亮色/暗色模式的 SVG 图表，输出为独立 HTML 文件，采用统一的教育视觉语言，包含 9 种语义色阶、句首大写排版及自动暗色模式。最适合教育和说明类内容。 |
| [**hyperframes**](/user-guide/skills/optional/creative/creative-hyperframes) | 使用 HyperFrames 创建基于 HTML 的视频合成、动态标题卡、社交叠层、字幕访谈视频、音频响应视觉效果及着色器转场。HTML 是视频的唯一来源。适用于用户希望制作任何视频内容的场景。 |
| [**kanban-video-orchestrator**](/user-guide/skills/optional/creative/creative-kanban-video-orchestrator) | 规划、搭建并监控由 Hermes Kanban 支撑的多 agent 视频制作流水线。适用于用户希望制作任何类型视频的场景 — 叙事影片、产品/营销视频、MV、解说视频、ASCII/终端艺术、抽象/生成式循环等。 |
| [**meme-generation**](/user-guide/skills/optional/creative/creative-meme-generation) | 通过选取模板并使用 Pillow 叠加文字来生成真实的 meme 图片，输出实际的 .png 文件。 |

## devops

| 技能 | 描述 |
|-------|-------------|
| [**inference-sh-cli**](/user-guide/skills/optional/devops/devops-cli) | 通过 inference.sh CLI（infsh）运行 150+ AI 应用 — 图像生成、视频创作、LLM、搜索、3D、社交自动化。使用终端工具。触发词：inference.sh、infsh、ai apps、flux、veo、图像生成、视频生成、seedrea 等。 |
| [**docker-management**](/user-guide/skills/optional/devops/devops-docker-management) | 管理 Docker 容器、镜像、卷、网络及 Compose 栈 — 生命周期操作、调试、清理及 Dockerfile 优化。 |
| [**pinggy-tunnel**](/user-guide/skills/optional/devops/devops-pinggy-tunnel) | 通过 Pinggy 经 SSH 实现零安装本地隧道。 |
| [**watchers**](/user-guide/skills/optional/devops/devops-watchers) | 轮询 RSS、JSON API 和 GitHub，并使用水印去重。 |

## dogfood

| 技能 | 描述 |
|-------|-------------|
| [**adversarial-ux-test**](/user-guide/skills/optional/dogfood/dogfood-adversarial-ux-test) | 扮演产品中最难应对的技术抵触型用户。以该角色浏览应用，找出所有 UX 痛点，再通过实用主义过滤层区分真实问题与噪音，生成可执行的工单。 |

## email

| 技能 | 描述 |
|-------|-------------|
| [**agentmail**](/user-guide/skills/optional/email/email-agentmail) | 通过 AgentMail 为 agent 提供专属邮箱。使用 agent 专属邮件地址（如 hermes-agent@agentmail.to）自主发送、接收和管理邮件。 |

## finance

| 技能 | 描述 |
|-------|-------------|
| [**3-statement-model**](/user-guide/skills/optional/finance/finance-3-statement-model) | 在 Excel 中构建完整集成的三表模型（利润表、资产负债表、现金流量表），包含营运资本计划、折旧摊销滚动、债务计划及使现金与留存收益平衡的勾稽项。与 excel-author 配合使用。 |
| [**comps-analysis**](/user-guide/skills/optional/finance/finance-comps-analysis) | 在 Excel 中构建可比公司分析 — 运营指标、估值倍数、与同行集合的统计基准对比。与 excel-author 配合使用。适用于上市公司估值、IPO 定价、行业基准或异常值检测。 |
| [**dcf-model**](/user-guide/skills/optional/finance/finance-dcf-model) | 在 Excel 中构建机构级 DCF 估值模型 — 收入预测、自由现金流构建、WACC、终值、悲观/基准/乐观情景及 5×5 敏感性分析表。与 excel-author 配合使用。适用于内在价值股权分析。 |
| [**excel-author**](/user-guide/skills/optional/finance/finance-excel-author) | 使用 openpyxl 无头构建可审计的 Excel 工作簿 — 蓝/黑/绿单元格规范、公式优先于硬编码、命名区域、余额校验、敏感性分析表。适用于财务模型、审计输出、对账。 |
| [**lbo-model**](/user-guide/skills/optional/finance/finance-lbo-model) | 在 Excel 中构建杠杆收购模型 — 资金来源与用途、债务计划、现金清偿、退出倍数、IRR/MOIC 敏感性分析。与 excel-author 配合使用。适用于 PE 筛选、主导方案估值或 pitch 中的示意性 LBO。 |
| [**merger-model**](/user-guide/skills/optional/finance/finance-merger-model) | 在 Excel 中构建增厚/摊薄（并购）模型 — 合并后利润表、协同效应、融资结构、每股收益影响。与 excel-author 配合使用。适用于并购 pitch、董事会材料或交易评估。 |
| [**pptx-author**](/user-guide/skills/optional/finance/finance-pptx-author) | 使用 python-pptx 无头构建 PowerPoint 演示文稿。与 excel-author 配合，制作每个数字均可追溯至工作簿单元格的模型支撑型幻灯片。适用于 pitch deck、投委会备忘录、盈利说明。 |
| [**stocks**](/user-guide/skills/optional/finance/finance-stocks) | 通过 Yahoo 获取股票报价、历史数据、搜索、对比及加密货币行情。 |

## health

| 技能 | 描述 |
|-------|-------------|
| [**fitness-nutrition**](/user-guide/skills/optional/health/health-fitness-nutrition) | 健身训练计划与营养追踪。通过 wger 按肌肉群、器械或类别搜索 690+ 种训练动作。通过 USDA FoodData Central 查询 380,000+ 种食物的宏量营养素和热量。计算 BMI、TDEE、单次最大重量、宏量营养素分配及体成分。 |
| [**neuroskill-bci**](/user-guide/skills/optional/health/health-neuroskill-bci) | 连接运行中的 NeuroSkill 实例，将用户的实时认知和情绪状态（专注度、放松度、情绪、认知负荷、困倦度、心率、HRV、睡眠分期及 40+ 项衍生 EXG 评分）融入响应中。 |

## mcp

| 技能 | 描述 |
|-------|-------------|
| [**fastmcp**](/user-guide/skills/optional/mcp/mcp-fastmcp) | 使用 Python 中的 FastMCP 构建、测试、检查、安装和部署 MCP 服务器。适用于创建新 MCP 服务器、将 API 或数据库封装为 MCP 工具、暴露资源或 prompt（提示词），或为 Claude Code、Cursor 等准备 FastMCP 服务器的场景。 |
| [**mcporter**](/user-guide/skills/optional/mcp/mcp-mcporter) | 使用 mcporter CLI 列出、配置、鉴权并直接调用 MCP 服务器/工具（HTTP 或 stdio），包括临时服务器、配置编辑及 CLI/类型生成。 |

## migration

| 技能 | 描述 |
|-------|-------------|
| [**openclaw-migration**](/user-guide/skills/optional/migration/migration-openclaw-migration) | 将用户的 OpenClaw 自定义配置迁移至 Hermes Agent。从 ~/.openclaw 导入兼容 Hermes 的记忆、SOUL.md、命令白名单、用户技能及选定的工作区资产，并报告无法迁移的内容。 |


## productivity

| 技能 | 描述 |
|-------|-------------|
| [**canvas**](/user-guide/skills/optional/productivity/productivity-canvas) | Canvas LMS 集成 — 使用 API token 认证获取已注册课程和作业。 |
| [**here.now**](/user-guide/skills/optional/productivity/productivity-here-now) | 将静态站点发布至 &#123;slug&#125;.here.now，并将私有文件存储在云端 Drive 中以供 agent 间交接。 |
| [**memento-flashcards**](/user-guide/skills/optional/productivity/productivity-memento-flashcards) | 间隔重复闪卡系统。从事实或文本创建卡片，通过 agent 评分的自由文本回答与闪卡对话，从 YouTube 字幕生成测验，使用自适应调度复习到期卡片，并支持导出/导入。 |
| [**shop**](/user-guide/skills/optional/productivity/productivity-shop) | Shop.app：商品搜索、订单追踪、退货、重新下单。 |
| [**shopify**](/user-guide/skills/optional/productivity/productivity-shopify) | 通过 curl 使用 Shopify Admin 和 Storefront GraphQL API。支持商品、订单、客户、库存、元字段。 |
| [**siyuan**](/user-guide/skills/optional/productivity/productivity-siyuan) | 通过 curl 使用 SiYuan Note API，在自托管知识库中搜索、读取、创建和管理块与文档。 |
| [**telephony**](/user-guide/skills/optional/productivity/productivity-telephony) | 为 Hermes 添加电话能力，无需修改核心工具。配置并持久化 Twilio 号码，发送和接收 SMS/MMS，拨打直接通话，并通过 Bland.ai 或 Vapi 发起 AI 驱动的外呼。 |

## research

| 技能 | 描述 |
|-------|-------------|
| [**bioinformatics**](/user-guide/skills/optional/research/research-bioinformatics) | 通往 bioSkills 和 ClawBio 400+ 生物信息学技能的入口。涵盖基因组学、转录组学、单细胞、变异检测、药物基因组学、宏基因组学、结构生物学等领域，按需获取特定领域参考资料。 |
| [**darwinian-evolver**](/user-guide/skills/optional/research/research-darwinian-evolver) | 使用 Imbue 的进化循环演化 prompt/正则表达式/SQL/代码。 |
| [**domain-intel**](/user-guide/skills/optional/research/research-domain-intel) | 使用 Python 标准库进行被动域名侦察。子域名发现、SSL 证书检查、WHOIS 查询、DNS 记录、域名可用性检测及批量多域名分析。无需 API 密钥。 |
| [**drug-discovery**](/user-guide/skills/optional/research/research-drug-discovery) | 药物发现工作流的制药研究助手。在 ChEMBL 上搜索生物活性化合物，计算类药性（Lipinski Ro5、QED、TPSA、合成可及性），通过 OpenFDA 查询药物相互作用，解读 ADMET 属性。 |
| [**duckduckgo-search**](/user-guide/skills/optional/research/research-duckduckgo-search) | 通过 DuckDuckGo 免费网络搜索 — 文本、新闻、图片、视频。无需 API 密钥。优先使用已安装的 `ddgs` CLI；仅在确认当前运行时中 `ddgs` 可用后才使用 Python DDGS 库。 |
| [**gitnexus-explorer**](/user-guide/skills/optional/research/research-gitnexus-explorer) | 使用 GitNexus 为代码库建立索引，并通过 Web UI + Cloudflare 隧道提供交互式知识图谱。 |
| [**osint-investigation**](/user-guide/skills/optional/research/research-osint-investigation) | 公开记录 OSINT 调查框架 — SEC EDGAR 文件、USAspending 合同、参议院游说记录、OFAC 制裁、ICIJ 离岸泄露、纽约市房产记录（ACRIS）、OpenCorporates 注册信息、CourtListener 法院记录、Wayback Machine 等。 |
| [**parallel-cli**](/user-guide/skills/optional/research/research-parallel-cli) | Parallel CLI 的可选厂商技能 — agent 原生网络搜索、提取、深度研究、数据增强、FindAll 及监控。优先使用 JSON 输出和非交互式流程。 |
| [**qmd**](/user-guide/skills/optional/research/research-qmd) | 使用 qmd（一款结合 BM25、向量搜索和 LLM 重排序的混合检索引擎）在本地搜索个人知识库、笔记、文档和会议记录。支持 CLI 和 MCP 集成。 |
| [**scrapling**](/user-guide/skills/optional/research/research-scrapling) | 使用 Scrapling 进行网页抓取 — 通过 CLI 和 Python 实现 HTTP 获取、隐身浏览器自动化、Cloudflare 绕过及爬虫抓取。 |
| [**searxng-search**](/user-guide/skills/optional/research/research-searxng-search) | 通过 SearXNG 免费元搜索 — 聚合 70+ 搜索引擎的结果。可自托管或使用公共实例。无需 API 密钥。当网络搜索工具集不可用时自动回退。 |

## security

| 技能 | 描述 |
|-------|-------------|
| [**1password**](/user-guide/skills/optional/security/security-1password) | 配置并使用 1Password CLI（op）。适用于安装 CLI、启用桌面应用集成、登录及为命令读取/注入密钥的场景。 |
| [**oss-forensics**](/user-guide/skills/optional/security/security-oss-forensics) | 针对 GitHub 仓库的供应链调查、证据恢复和取证分析。涵盖已删除提交恢复、强制推送检测、IOC 提取、多源证据收集、假设形成/验证等。 |
| [**sherlock**](/user-guide/skills/optional/security/security-sherlock) | 跨 400+ 社交网络的 OSINT 用户名搜索。通过用户名追踪社交媒体账号。 |

## software-development

| 技能 | 描述 |
|-------|-------------|
| [**rest-graphql-debug**](/user-guide/skills/optional/software-development/software-development-rest-graphql-debug) | 调试 REST/GraphQL API：状态码、认证、schema、问题复现。 |

## web-development

| 技能 | 描述 |
|-------|-------------|
| [**page-agent**](/user-guide/skills/optional/web-development/web-development-page-agent) | 将 alibaba/page-agent 嵌入您自己的 Web 应用 — 一个纯 JavaScript 页内 GUI agent，以单个 `<script>` 标签或 npm 包形式提供，让您网站的终端用户可以用自然语言驱动 UI（如"点击登录，填写用户名..."）。 |

---

## 贡献可选技能

向仓库添加新的可选技能：

1. 在 `optional-skills/<category>/<skill-name>/` 下创建目录
2. 添加包含标准 frontmatter 的 `SKILL.md`（name、description、version、author）
3. 在 `references/`、`templates/` 或 `scripts/` 子目录中包含所有支撑文件
4. 提交 pull request — 合并后该技能将出现在本目录并获得专属文档页面