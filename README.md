# Loon Rules

这是一个自动收集、合并、清洗、去重并定期更新 Loon 分流规则的仓库。所有生成规则统一位于 `rule/`，由同一套配置、更新脚本、测试和 GitHub Actions 管理。

规则主要来自：

- [v2fly/domain-list-community](https://github.com/v2fly/domain-list-community)
- [blackmatrix7/ios_rule_script](https://github.com/blackmatrix7/ios_rule_script)
- 少量经审核、用于补足专用服务的本地规则

## 主要功能

- 自动下载并校验上游规则，支持失败重试和最低规则数保护。
- 递归解析 v2fly `include:`，检测循环引用，并支持按服务排除不应展开的分类。
- 合并 Blackmatrix7 主规则、Domain、Resolve 等分片以及 v2fly 专用分类。
- 转换为标准 Loon Rule，过滤空行、无效项并按完整语义去重。
- 保护服务边界，避免 Google / YouTube / Gemini、Apple / ApplePush 等规则无边界混合。
- 每 6 小时由 GitHub Actions 自动检查更新，也支持手动触发。
- 规则内容没有变化时保留原时间戳，不写文件、不创建空提交。
- 全量下载和校验成功后才原子写入，单一上游异常不会清空或部分覆盖现有规则。
- 通过单元测试检查格式、递归、隔离、目录漂移、失败保护和工作流配置。

## 当前规则

当前共有 24 个自动维护的规则文件：

| 分类 | 规则 |
| --- | --- |
| AI | `Anthropic`、`Gemini`、`OpenAI` |
| Apple | `Apple`、`ApplePush` |
| Google | `Google`、`GoogleVoice`、`YouTube` |
| 社交与通信 | `Instagram`、`Reddit`、`Telegram`、`TikTok`、`Twitter`、`WhatsApp` |
| 开发与平台 | `GitHub`、`Microsoft` |
| 金融、交易与电商 | `Binance`、`Bybit`、`eBay`、`OKX`、`PayPal`、`Wise` |
| 区域与网络工具 | `ChinaMax`、`ip-query` |

实际文件列表以 [`rule/`](rule/) 和 [`config/rules.json`](config/rules.json) 为准；测试会阻止配置与生成目录发生遗漏或漂移。

## 使用方法

在 Loon 中引用对应规则的 GitHub Raw 地址：

```text
https://raw.githubusercontent.com/villwong/loon-rules/main/rule/<文件名>.list
```

示例：

```text
https://raw.githubusercontent.com/villwong/loon-rules/main/rule/OpenAI.list
https://raw.githubusercontent.com/villwong/loon-rules/main/rule/TikTok.list
https://raw.githubusercontent.com/villwong/loon-rules/main/rule/ApplePush.list
https://raw.githubusercontent.com/villwong/loon-rules/main/rule/ip-query.list
```

`rule/` 中的文件是生成结果，不应直接编辑。需要长期保留的补充项应放入 `config/supplements/`，再登记到 `config/rules.json`。

## IP Query 维护策略

`rule/ip-query.list` 专门收录 IP 查询、IP 地理位置、出口 IP、IPv4 / IPv6、DNS 泄漏与浏览器泄漏检测服务。更新器使用 v2fly 的独立 `category-ip-geo-detect` 分类作为持续发现和更新的主来源，并合并 `config/supplements/IPQuery.list` 中经人工审核、但上游尚未覆盖的站点。

该规则启用额外的严格策略：

- 只接受 `DOMAIN` 与 `DOMAIN-SUFFIX`。
- 校验域名标签、长度和基本结构，拒绝通配符、IP、单标签及明显错误域名。
- 同一域名同时出现 `DOMAIN` / `DOMAIN-SUFFIX` 时优先保留后者。
- 按域名稳定排序，确保相同输入产生相同结果。
- 不使用搜索引擎 HTML 爬虫，不自动混入测速站、VPN 官网、CDN、代理商或广告域名。

## Apple 与 ApplePush

`Apple.list` 合并 Blackmatrix7 的 Apple 主规则、Domain、Resolve 分片和 v2fly Apple 分类，覆盖 Apple ID、iCloud、App Store、媒体、更新及常用系统服务。

`ApplePush.list` 独立维护，不会导入完整 Apple 生态。两大规则项目目前没有独立 APNs 分类，因此它只使用经审核的 APNs 域名及 Apple 官方公布的 IPv4 / IPv6 网段。参考：[Apple APNs 网络要求](https://support.apple.com/102266) 与 [Apple 企业网络主机列表](https://support.apple.com/101555)。

## 自动更新

工作流位于 `.github/workflows/update-rules.yml`：

1. 每 6 小时由 `schedule` 自动运行，也可通过 `workflow_dispatch` 手动执行。
2. 运行全部单元测试。
3. 下载并准备所有服务；任一来源失败、截断或低于最低规则数时整次更新失败，现有输出保持不变。
4. 生成并再次验证规则。
5. 只暂存 `rule/`；若内容无变化则不提交，否则提交 `chore: update Loon rules`。

本地验证：

```bash
python -m unittest discover -s tests -v
python scripts/update_rules.py
python -m unittest discover -s tests -v
```

## 项目结构

```text
.github/workflows/       GitHub Actions 定时与手动更新流程
config/rules.json        服务、输出、来源、最低数量和隔离策略
config/supplements/      经审核的本地补充规则，不是最终订阅入口
rule/                    自动生成的 Loon 规则
scripts/update_rules.py  下载、递归解析、转换、过滤、合并与原子写入
tests/                   单元测试与仓库一致性检查
```

## 数据来源与边界

本项目不会简单复制单一大合集，而是按服务选择专用来源。v2fly 的递归 `include:` 会保留其语义，但可通过 `exclude_includes` 阻止子服务被母分类吞并；Blackmatrix7 的分片只在同一服务内部合并。Google / YouTube / Gemini、Microsoft / GitHub、Apple / ApplePush、AI 服务以及 TikTok 等均保持独立输出。

## 注意事项

- 规则来自公开项目和公开资料，上游结构、内容及可用性可能变化。
- 自动生成结果会经过保护性校验，但不保证永久完整或适合所有网络环境。
- 如发现遗漏、误收或上游变化，欢迎提交 Issue 或 Pull Request。
