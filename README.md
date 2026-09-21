# loon-rules

自动整理常用 Loon 分流规则，数据主要来自 v2fly 和 BlackMatrix7，每 6 小时检查更新。

## 规则

所有订阅文件位于 [`rule/`](rule/)。

### Google

| 文件 | 内容 |
| --- | --- |
| [`Google.list`](rule/Google.list) | Google 全生态规则 |
| [`Gemini.list`](rule/Gemini.list) | Gemini、Google AI Studio、Gemini API、DeepMind 与 Bard 兼容域名 |
| [`YouTube.list`](rule/YouTube.list) | YouTube、YouTube Music、视频 CDN 与 API |
| [`GoogleDrive.list`](rule/GoogleDrive.list) | Google Drive、Docs 文件访问与 Google Photos |
| [`GoogleVoice.list`](rule/GoogleVoice.list) | Google Voice 与 Telephony |
| [`GooglePlay.list`](rule/GooglePlay.list) | Google Play、应用下载与 Play 服务 |

Google 子规则都包含在 `Google.list` 中。需要单独分流时，把子规则放在 `Google.list` 前面；不需要细分时，只使用 `Google.list`。

### Apple

| 文件 | 内容 |
| --- | --- |
| [`Apple.list`](rule/Apple.list) | Apple 全生态规则 |
| [`ApplePush.list`](rule/ApplePush.list) | Apple Push Notification Service（APNs） |
| [`AppStore.list`](rule/AppStore.list) | App Store 与应用下载更新 |
| [`iCloud.list`](rule/iCloud.list) | iCloud、iCloud Drive、照片与同步服务 |
| [`AppleMusic.list`](rule/AppleMusic.list) | Apple Music |
| [`AppleTV.list`](rule/AppleTV.list) | Apple TV 与 Apple TV+ |
| [`AppleNews.list`](rule/AppleNews.list) | Apple News |
| [`Siri.list`](rule/Siri.list) | Siri |
| [`TestFlight.list`](rule/TestFlight.list) | TestFlight |
| [`AppleProxy.list`](rule/AppleProxy.list) | 通常需要境外网络访问的 Apple 服务聚合规则 |

Apple 子规则都包含在 `Apple.list` 中。`AppleProxy.list` 可以与 Apple TV、Apple News 和 iCloud Private Relay 规则重叠，但不包含普通 App Store、APNs、系统升级和通用 Apple CDN。

### AI

| 文件 | 内容 |
| --- | --- |
| [`AI.list`](rule/AI.list) | OpenAI、Anthropic 与 Gemini 聚合规则 |
| [`OpenAI.list`](rule/OpenAI.list) | OpenAI 与 ChatGPT |
| [`Anthropic.list`](rule/Anthropic.list) | Anthropic 与 Claude |

`Gemini.list` 同时属于 Google 和 AI，因此也包含在 `AI.list` 中。

### 社交与通信

- [`Instagram.list`](rule/Instagram.list)
- [`Reddit.list`](rule/Reddit.list)
- [`Telegram.list`](rule/Telegram.list)
- [`TikTok.list`](rule/TikTok.list)
- [`Twitter.list`](rule/Twitter.list)
- [`WhatsApp.list`](rule/WhatsApp.list)

### 金融、交易与电商

| 文件 | 内容 |
| --- | --- |
| [`Crypto.list`](rule/Crypto.list) | Binance、Bybit 与 OKX 聚合规则 |
| [`Binance.list`](rule/Binance.list) | Binance |
| [`Bybit.list`](rule/Bybit.list) | Bybit |
| [`OKX.list`](rule/OKX.list) | OKX |
| [`PayPal.list`](rule/PayPal.list) | PayPal |
| [`Wise.list`](rule/Wise.list) | Wise |
| [`eBay.list`](rule/eBay.list) | eBay |

交易所和支付服务继续保留独立文件；聚合表不会替代 App 子表。

### 开发与平台

- [`GitHub.list`](rule/GitHub.list)
- [`Microsoft.list`](rule/Microsoft.list)：Microsoft 全生态规则，包含 GitHub 子表

### 其他

- [`ChinaMax.list`](rule/ChinaMax.list)：中国大陆域名与 IP 聚合规则
- [`ip-query.list`](rule/ip-query.list)：IP、DNS 与 WebRTC 泄漏检测服务

## 使用

复制对应文件的 Raw 地址添加到 Loon：

```text
https://raw.githubusercontent.com/villwong/loon-rules/main/rule/文件名.list
```

精细分流时，子规则放在总规则前面。例如：

```text
Gemini  → 美国节点
YouTube → YouTube 策略
Google  → Google 默认策略
```

## 数据来源

- [v2fly/domain-list-community](https://github.com/v2fly/domain-list-community)
- [blackmatrix7/ios_rule_script](https://github.com/blackmatrix7/ios_rule_script)
- `config/supplements/` 中经过确认的补充规则

## 更新

GitHub Actions 每 6 小时自动检查一次，也支持手动运行。更新流程会先下载并校验全部上游，再生成、规范化和测试所有规则；任一上游失败时不会用空数据或残缺数据覆盖现有文件。

只有规则内容发生变化时才会提交更新。

本地运行：

```bash
python scripts/update_rules.py
python -m unittest discover -s tests -v
```
