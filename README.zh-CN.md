# OneCo

[English](README.md)

OneCo 是在 Mac 上运行的本地工具，供一个人运营 AI 原生产品公司。你担任 Board，常驻的 CEO 和 CTO 协助确定产品方向与技术边界。每个产品只有一个 Owner，负责执行已经达成共识的工作并验证结果。

OneCo 不是云服务。公司决策、产品 Brief、规格和代码都保存在 Mac 本地的普通文件与 Git 仓库中，而不是封闭数据库里。SQLite 只用于本地协调。

> **当前状态：** v0.2 是早期 macOS 版本，依赖 TraeCode、Ghostty 和 tmux。安装前请先确认[环境要求](#环境要求)。

## 它如何工作

```text
你（Board）
  ├─ 和 CEO 讨论用户、范围与优先级
  └─ 和 CTO 讨论架构、质量与风险
          │
          ├─ 两者共同完善并确认产品启动材料
          ▼
     一个 Owner 在对应产品仓库内执行
          │
          └─ 测试、检查点、决策与证据都留在本地文件中
```

- **Board：** 就是你。你决定公司方向，并处理重要取舍。
- **CEO：** 确保产品工作不偏离用户意图、范围、先后顺序和推进节奏。
- **CTO：** 确保架构合理，并让实现保持正确、安全且易于维护。
- **Owner：** 单个产品唯一拥有正式写入权的角色。它执行已接受的规格、验证结果、记录检查点，然后继续下一个兼容任务。
- **Cockpit：** 一个轻量的原生公司状态面板，不是独立的项目管理系统。

CEO 和 CTO 各自在独立的 Ghostty 窗口中运行。Owner 在隐藏的 tmux 会话里工作，因此关掉窗口不会中断任务。仓库内置的 Trae 插件提供角色说明和经过授权的 OneCo 工具。每个角色能做什么，由运行时身份决定。

## 环境要求

- macOS 13 或更高版本
- Python 3.11 或更高版本
- [`uv`](https://docs.astral.sh/uv/)
- Git
- tmux
- Ghostty 1.3 或更高版本
- TraeCode CLI：已登录，并能使用配置的模型（默认 `GPT-5.6-Sol`）

OneCo 目前只支持 macOS，因为 Cockpit 和高管窗口集成依赖 macOS 原生 API 与 Ghostty 自动化。

## 安全提示

OneCo 启动的本地 Agent 会沿用当前 TraeCode 的权限。使用前请确认这些权限符合预期，并只在可信仓库中运行 OneCo。

## 安装

```bash
git clone https://github.com/Dong237/oneco.git
cd oneco
uv tool install .
oneco --version
```

如果要在本地开发 OneCo，而不是把它安装成工具：

```bash
uv sync --group dev
uv run oneco --version
```

## 创建公司

```bash
oneco create ~/my-company --name "My Company"
```

如果你的 TraeCode 账号不能使用默认模型，请明确指定账号中已有的模型：

```bash
oneco create ~/my-company --name "My Company" --model "MODEL_NAME"
```

模型不可用时，OneCo 会直接报错，不会悄悄替换成其他模型。

这条命令会检查配置的 Trae 模型、在本地安装仓库内置的 OneCo 插件、创建公司仓库、初始化 Git，并在 `~/Applications` 中添加 `OneCo — My Company.app`。

检查环境：

```bash
oneco doctor --root ~/my-company
```

之后可以从 Finder 启动，也可以运行：

```bash
oneco start --root ~/my-company
```

## 日常用法

1. 告诉 CEO 你想为谁做什么，以及最先需要交付什么结果。
2. 请 CTO 找出最简单且技术上可靠的方案，并明确质量底线。
3. 请 CEO 和 CTO 一起完成产品 Brief、第一份 Spec、项目宪章、验收证据和明确的非目标。
4. 等两人确认同一份启动材料后，请他们启动产品。
5. 接下来由 Owner 执行。你可以在 Cockpit 中查看进度；产品方向需要调整时找 CEO，技术方向需要调整时找 CTO。
6. 随时可以查看公司状态：

```bash
oneco status --root ~/my-company
```

日常使用时，你不需要亲自管理 tmux、会话、队列或内部消息。相关命令主要用于检查、自动化和故障恢复。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `oneco start --root ~/my-company` | 打开或聚焦 Cockpit、CEO 和 CTO |
| `oneco status --root ~/my-company` | 查看项目、身份、未读消息和待处理动作 |
| `oneco talk CEO --root ~/my-company` | 聚焦或恢复同一条 CEO 对话 |
| `oneco talk CTO --root ~/my-company` | 聚焦或恢复同一条 CTO 对话 |
| `oneco branch CEO --purpose "pricing" --root ~/my-company` | 创建顾问性质的高管对话分支，不改变正式权限归属 |
| `oneco doctor --root ~/my-company` | 检查宿主环境、插件、模型和会话环境 |
| `oneco runtime rebuild --root ~/my-company` | 根据版本化文件重建可丢弃的运行时状态 |

完整命令请运行 `oneco --help` 或 `oneco COMMAND --help`。

## 在不同 Mac 之间迁移

OneCo 公司包含一个控制仓库，以及直接放在其中的多个独立产品仓库：

```text
my-company/
├── .oneco/                  本地配置与可重建运行时
├── COMPANY.md               公司章程和工作原则
├── roles/                   CEO、CTO 与 Owner 的权限边界
├── portfolio/               项目登记、决策与审批
├── playbook/                已验证的可复用实践
├── first-product/           独立 Git 仓库
└── second-product/          独立 Git 仓库
```

请提交公司控制文件和每个产品仓库，但不要提交 `.oneco/runtime.sqlite`、socket、日志或备份。换到另一台 Mac 时，按相同结构克隆这些仓库，再运行 `oneco runtime rebuild`。Markdown、JSON 和 Git 历史才是需要长期保存的记录。

## 文档

- [安装与升级](docs/installation.md)
- [Board 日常操作](docs/board-operations.md)
- [系统架构](docs/architecture.md)
- [运行协议](docs/protocol.md)
- [Spec Kit 集成](integrations/speckit/README.md)

## 开发

```bash
uv sync --group dev
uv run ruff check .
uv run pytest
uv build
traecli plugin validate --path adapters/trae-plugin/oneco
```

提交改动前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

[MIT](LICENSE)
