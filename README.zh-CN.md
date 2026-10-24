# OneCo

[English](README.md)

OneCo 是给单人 AI 原生公司用的本地工作系统。你是 Board；常驻的 CPO、CEO 和 CTO 负责调研机会、定义产品与技术边界；每个产品只有一个 Owner，负责实现和验证。

OneCo 的运行时和公司记录都留在你的 Mac 上。Signal、Product Bet、决策、规格、检查点、证据和代码保存在普通文件与 Git 仓库中。SQLite 只存本地协作状态，OneCo 不提供云端控制服务。

> **当前状态：** v1.0 已实现从 Signal 到发布的完整流程，目前只支持 macOS。安装前请先确认[环境要求](#环境要求)。

## 工作流程

```text
Board（你）
  │
  ├─ 分配一条可追溯的 Signal
  ▼
CPO 调研并提交 Product Bet
  │
  ├─ Board 选择、拒绝，或要求补充证据
  ▼
CEO 将入选 Bet 的精确版本转成产品仓库
  │
  ├─ CEO 和 CTO 共同整理启动材料
  ├─ 两人确认同一份启动材料的哈希摘要
  ▼
Owner 开发并验证；CTO 监督关键节点
  │
  ├─ Owner 登记精确的发布候选版本
  ├─ CEO 和 CTO 确认同一个发布版本的哈希摘要
  ▼
Board 批准或拒绝上线
  │
  └─ Owner 执行获批的发布并记录结果
```

| 角色 | 职责 |
| --- | --- |
| **Board** | 就是你。分配 Signal、决定 Product Bet、批准上线，并处理重大冲突。 |
| **CPO** | 只研究 Board 已分配的 Signal，整理有证据支撑的 Product Bet。CPO 是顾问角色，不能创建项目或指挥 Owner。 |
| **CEO** | 将入选 Bet 转成项目，负责产品意图、范围、顺序和验收标准。 |
| **CTO** | 设计技术路径，监督架构、正确性、安全性和可维护性。 |
| **Owner** | 单个产品唯一的正式写入者，负责执行、验证、记录检查点和发布证据。 |
| **Cockpit** | Board 的原生控制面板，用于查看角色、Signal、Bet、产品、健康状态和待决事项。 |

**Daily Product Teardown 只是可选的外部来源。** 它不内置在 OneCo 中，也不是 CPO 的固定步骤。如果你已经在外部使用它，可以把人工审阅后的结果作为一份可追溯的 Signal 来源。OneCo 不会自动安排或调用 Daily Product Teardown。

## 环境要求

- macOS 13 或更高版本
- Python 3.11（暂不支持 Python 3.12 及以上版本，因为原生 Cockpit 在这些版本中无法可靠渲染）
- [`uv`](https://docs.astral.sh/uv/)
- Git
- tmux
- Ghostty 1.3 或更高版本
- TraeCode CLI：已登录，并能使用配置的模型（默认 `GPT-5.6-Sol`）

只有使用 `oneco spec ...` 命令时才需要额外安装 Spec Kit 的 `specify` CLI。Cockpit 和角色窗口依赖 macOS 原生 API 与 Ghostty 自动化，因此 OneCo 目前只支持 macOS。首次使用时，系统可能会询问是否允许控制 Ghostty。

## 安全提示

OneCo 默认让 CPO、CEO、CTO 和 Owner 的新建、恢复、分支及执行轮次使用 Trae Full Access（`bypass_permissions`），避免日常工具调用反复请求授权。请只在可信仓库中运行 OneCo。如需改用 Trae 自动判断权限，可在启动或打开角色时传入 `--permission auto`；Supervisor 会把该模式传给 Owner。

## 安装

```bash
git clone https://github.com/Dong237/oneco.git
cd oneco
uv tool install --python 3.11 .
oneco --version
```

如果要在本地开发 OneCo：

```bash
uv sync --group dev
uv run oneco --version
```

## 创建并启动公司

```bash
oneco create ~/my-company --name "My Company"
```

如果 TraeCode 账号不能使用默认模型，请明确指定账号已有的模型：

```bash
oneco create ~/my-company --name "My Company" --model "MODEL_NAME"
```

OneCo 会在写入公司文件前检查指定模型，绝不会悄悄换成其他模型。这条命令还会安装本地 OneCo 插件、创建公司控制仓库、初始化 Git，并在 `~/Applications` 中添加 `OneCo — My Company.app`。

先检查环境，再启动：

```bash
oneco doctor --root ~/my-company
oneco start --root ~/my-company
```

你也可以在 Finder 中双击生成的应用。再次启动时，OneCo 会聚焦已有的 Cockpit，不会重复打开。

## Cockpit

Cockpit 是 Board 日常使用的入口，可以查看和处理：

- CPO、CEO 和 CTO 的状态，并聚焦或恢复各自的对话；
- 已分配的 Signal、Product Bet 及其当前决策状态；
- Product Bet 的选择、拒绝和补充证据请求；
- 已选产品、当前任务、Owner 状态，以及只读的 **Watch** 窗口；
- 系统健康、阻塞项和等待上线决策的发布候选版本。

它是控制面板，不是另一套项目管理工具。产品讨论发生在角色对话里，实现留在各自的产品仓库中。

## v1.0 日常流程

### 1. 分配 Signal

先提交一条有可追溯来源的观察、问题或机会。可以点击 Cockpit 中的 **Assign signal**，也可以运行：

```bash
oneco signal add \
  --title "Release confidence" \
  --summary "Independent developers need a reliable release gate" \
  --source-uri "https://example.com/source" \
  --root ~/my-company
```

只有 Board 能分配 Signal。没有明确分配时，CPO 不会自行发起调研。

### 2. 由 CPO 整理 Product Bet

CPO 围绕已分配的 Signal 调研并记录：用户与 Job、替代方案、带日期的来源、反证、未知项、产品机会、商业假设、适用时的验证结果，以及建议和相应的置信边界。

准备完成后，CPO 提交一份不可修改的 Product Bet 版本。验证结果只是证据，不等于批准。

### 3. 由 Board 决策

Board 在 Cockpit 中对当前提交的精确版本和摘要做出三种选择：

- **Select**：把 Bet 交给 CEO 立项。
- **More evidence**：指出缺少哪份证据；研究方法由 CPO 决定，之后提交新版本。
- **Reject**：保留已有证据和决策记录，但不创建项目。

选中 Product Bet 并不会立即创建项目。v1.0 的每个项目都必须由 CEO 从 Board 已选的 Bet 中创建。

### 4. CEO 立项，CEO 与 CTO 共同定稿

CEO 只能把 Board 选中的精确 Bet 版本转成独立产品仓库。入选的 Product Bet、版本清单和 Board 决策都会随项目保存。

CEO 负责产品定义、范围、验收示例、先后顺序和非目标；CTO 补充项目工程原则、技术约束和质量底线。两人重新读取完整启动材料，并分别确认同一个哈希摘要。任何启动文件发生变化，已有确认都会失效。

两份确认都有效后，CEO 或 CTO 才能启动 Owner。

### 5. Owner 执行，CTO 监督

Owner 是产品唯一的正式写入者。它每次领取一个已接受的任务，完成最小可用结果，验证、记录检查点、读取排队中的指令，然后继续不冲突的工作。

产品偏移由 CEO 处理。CTO 按监督周期检查活跃项目，也会在正确性、安全、架构和发布等关键节点介入。修正指令会等到 Owner 当前这一轮工作结束后再送达，OneCo 不会向运行中的 Agent 强行输入。发现风险时，CTO 只停止受影响的工作，不会暂停整个产品。

### 6. Board 批准上线

当一个增量满足验收条件后，Owner 会登记发布候选版本，并绑定精确的 Git commit 与证据文件。CEO 检查产品验收，CTO 检查技术证据；两人的确认必须指向同一个发布版本哈希摘要。

两份确认齐全后，Cockpit 才会要求 Board 决定是否上线。Board 对该候选版本作出批准或拒绝决定。批准后，系统只向 Owner 排入一次生产发布任务；Owner 执行后必须把结果证据写入文件。

OneCo 不会自动向任何远程仓库执行 `git push`。远程仓库和推送仍由你负责。

## 会话、停止与恢复

CPO、CEO 和 CTO 各自使用一个常驻 Trae 对话和独立 Ghostty 窗口。关掉窗口不会丢失对话；再次聚焦该角色时，OneCo 会恢复绑定的精确 UUID。Owner 也会恢复自己的精确线程，不会猜测“最近一次会话”。

只关闭 Cockpit 不会停止后台监督。需要停止时请运行：

```bash
oneco stop --root ~/my-company
```

之后运行 `oneco start --root ~/my-company` 即可重新启动。

恢复时以磁盘上的版本化记录为准：

- Owner 窗格意外退出时，已领取的工作会重新排队，并恢复原来的 Owner 线程。
- 本地运行时丢失时，运行 `oneco runtime rebuild --root ~/my-company`，再启动 OneCo。
- Finder 启动器丢失时，运行 `oneco create-launcher --root ~/my-company`。
- CPO、CEO 或 CTO 的 Trae 对话线程确实无法恢复时，运行 `oneco branch ROLE --purpose "recover ROLE" --recovery-candidate --root ~/my-company` 创建恢复候选。只有在该候选对话中输入系统给出的精确、限时确认短语，才会完成身份接替。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `oneco start --root ~/my-company` | 启动后台监督，并打开或聚焦 Cockpit 与常驻角色 |
| `oneco stop --root ~/my-company` | 停止后台监督 |
| `oneco status --root ~/my-company` | 查看角色、产品、未读消息、工作队列和待处理事项 |
| `oneco talk CPO --root ~/my-company` | 聚焦或精确恢复常驻角色；`CEO` 和 `CTO` 用法相同 |
| `oneco branch CEO --purpose "pricing" --root ~/my-company` | 新开顾问对话，不改变正式权限归属 |
| `oneco doctor --root ~/my-company` | 检查宿主环境、插件、模型、角色会话和运行时环境 |
| `oneco runtime rebuild --root ~/my-company` | 根据版本化记录重建可恢复的运行时状态 |

完整命令请运行 `oneco --help` 或 `oneco COMMAND --help`。

## 文件与 Git

公司本身是一个控制仓库，入选产品则各自使用直接位于其中的独立 Git 仓库：

```text
my-company/
├── .oneco/                  本地配置与可丢弃的运行时
├── COMPANY.md               公司章程和运行原则
├── roles/                   CPO、CEO、CTO 与 Owner 的权限边界
├── signals/                 Board 分配记录与来源清单
├── bets/                    版本化 Product Bet 与 Board 决策
├── portfolio/               入选产品登记与审批
├── operations/              监督与发布记录
├── playbook/                已验证的可复用实践
└── first-product/           独立 Git 仓库
```

请提交公司控制文件和每个产品仓库，但不要提交 `.oneco/runtime.sqlite`、socket、日志或备份。换到另一台 Mac 时，按相同的直接子目录结构克隆这些仓库，再运行 `oneco runtime rebuild`。Markdown、JSON、证据文件和 Git 历史才是可迁移的长期记录。

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
