# 第三方组件与服务

本仓库的作者代码以 MIT 许可发布（见 [LICENSE](LICENSE)）。下面是仓库内涉及或依赖的第三方内容。

## 随仓库分发

| 内容 | 位置 | 说明 |
|---|---|---|
| 腾讯会议 MCP 相关说明文档 | `tools/tencent_meeting/REFERENCE.md` | 腾讯会议官方工具集的接口说明，**版权归腾讯所有**，此处随附仅为方便使用者对照接口；如权利人要求，会立即移除并改为外链。使用腾讯会议相关能力须遵守腾讯的服务条款。 |
| 预编译的 macOS 语音识别工具 | `asr/*_bin` | 由同目录 `asr/*.swift` 源码编译（arm64）。源码与二进制均随本仓库以 MIT 分发；自行编译见 `asr/build.sh`。 |

## 外部服务（不随仓库分发，需自备凭证）

| 服务 | 用途 | 条款 |
|---|---|---|
| 阿里云百炼（DashScope） | 语音识别、热词表、可选 LLM | 阿里云服务条款，需自备 API Key |
| DeepSeek | 语义识别、校对、纪要生成 | DeepSeek 服务条款，需自备 API Key |
| 腾讯会议 | 可选：拉取会议转写稿 | 腾讯服务条款，需自备 Token |

## Python 依赖

见 `requirements.txt`（FastAPI/uvicorn、xlrd/openpyxl 等），各自适用其原始许可。
