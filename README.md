# C++ Library Manager

把第三方 C++ 库收集到当前目录，并提供一个本地网页控制台做手动收集、更新和移除。

## 它做什么

核心动作只有一个：**把远程仓库收成本地 git submodule**。

```
输入一个仓库地址  →  git submodule add  →  ./external/<库名>/
                  →  探测元信息（分支/commit/许可证/完整度）
                  →  写入 ./data/libraries/libraries.json
```

配套一个本地服务 + 网页，让你不用敲命令就能完成这件事。

## 快速开始

```bash
# 1. 启动（默认 http://127.0.0.1:8080，会自动打开浏览器）
./scripts/gui-interface.sh

# 或直接跑服务
python server.py --port 8080
```

打开页面后：

1. 左侧「手动收集」填入仓库地址，例如 `https://github.com/gabime/spdlog.git`
2. 目录名会自动从 URL 推导，可改；分支留空则用默认分支
3. 点「开始收集」，克隆日志会实时显示在页面底部
4. 右侧卡片上可以「更新」（`git submodule update --remote`）或「移除」

也可以点「推荐库」里的标签，自动填入表单。

## 自动筛选

不用一个个找库：页面下方「自动筛选」区会**同时查询多个来源**，按规则逐条判定，通过的勾选后一键批量收集。

### 数据源（严格对齐 SKILL.md 的「库收集策略」）

| 源 | 内容 | 规模 | 说明 |
|---|---|---|---|
| GitHub 搜索 | 按关键词/topic/star 搜索 | 实时 | 唯一能拿到 star 数和活跃度的源，但 API 额度紧 |
| vcpkg | 微软 vcpkg 端口库 | 2892 个 | 有描述和许可证，按包名匹配 |
| Conan Center | conan-center-index 配方 | 1973 个 | 从 `conandata.yml` 反查上游仓库，许可证/描述取自 `conanfile.py` |
| 官方/标准扩展 | `data/catalog/official.json` | 手工维护 | Boost、Google、标准库扩展 |

正好对应策略里的三类：**GitHub 仓库 / 官方仓库 / 包管理器**。

> **awesome-cpp 之类的二手清单没有收录**：它不在策略里，条目本身也指向 GitHub，
> 只是少了 star、活跃度和许可证——拿它当源等于用残缺数据重复 GitHub 已有的结果。
>
> **apt / Brew 也没有纳入**：它们分发的是二进制包而不是可克隆仓库，无法以 git submodule 形式收进 `external/`，与"收集"这个目标不符。

同一个库在多个源命中时会**自动合并**（例如 `glog` 同时出现在 vcpkg、Conan、官方清单里），许可证、描述、star 数互补，来源标签全部保留。

### 筛选条件

| 条件 | 说明 |
|---|---|
| 关键词 | 仓库名/描述/分类中的词，如 `json`、`http`、`log` |
| Topic | GitHub topic，逗号分隔，如 `header-only, cli` |
| 最小 star | 低于该值的淘汰；star 未知的源只提示不淘汰 |
| 最近更新（天） | 超过该天数没 push 的淘汰 |
| 体积上限（KB） | 防止误收巨型仓库，0 = 不限 |
| 数据源 | 上面 4 个源可任意勾选组合 |

> **star 和更新天数不写进 GitHub 查询**，而是在结果里逐条判定。
> 这样"GitHub 共 108 条 / 通过 15 条"两个数字都看得到，不会出现
> "明明有很多库，界面却只给 9 条"的困惑——被条件刷掉的条目会以半透明列出并注明原因。

固定排除 **已归档** 和 **fork** 的仓库。

**许可证不参与筛选**：GPL / LGPL / 未知许可证的库同样会出现在结果里，卡片上标出 SPDX 标识，能不能用由你自己判断（这条规则对 `header-only` 的模板库尤其重要——很多优质库是 BSL-1.0 或自定义许可）。

筛选完成后只会**自动勾选通过且评分 ≥60（良好及以上）**的条目；分数低的虽然通过硬性条件，但元信息太少，需要你手动确认。

### 评分与分档

评分 0-100，维度对齐 SKILL.md 的质量评估：

| 维度 | 分值 | 依据 |
|---|---|---|
| 热度 | 25 | star 数分档（5000+ / 3000+ / 1000+ / 500+ / 100+） |
| 活跃度 | 25 | 距上次 push ≤90 天满分，≤180 天 20，≤365 天 15 |
| 许可证 | 20 | 有可识别的开源许可证（只加分，不做门槛） |
| 描述完整 | 15 | 描述 ≥20 字符 |
| 来源权威度 | 15 | 官方 15 / vcpkg·conan 10 / GitHub 5 |

分档：**优秀 ≥80 · 良好 60-79 · 需要改进 <60**。若 star 数拿不到（vcpkg / Conan / 官方清单不提供），标为 **数据不足** 而不是硬判为差——这类库仍可手动勾选收集。

未通过的候选不会消失，会以半透明列出并写明原因（例如「已 900 天未更新（上限 730 天）」「star 12 低于门槛 100」）。

> **配额提示**：GitHub 搜索接口未认证时限制较严。设置环境变量 `GITHUB_TOKEN` 可大幅提高额度：
> ```bash
> GITHUB_TOKEN=ghp_xxx ./scripts/gui-interface.sh
> ```
> 其余四个源走 raw.githubusercontent / Git 树接口，不消耗搜索配额。

### 环境要求

- **Git** 2.0+（必须）
- **Python** 3.8+（只用标准库，无需 pip install）

## 收集后的目录

```
cpp-library-manager/
├── server.py                    # 本地服务（标准库实现，零依赖）
├── web-interface.html           # 控制台界面
├── SKILL.md                     # 收集策略说明（来源识别 / 质量分档）
├── .gitmodules                  # 由 git 自动维护
├── external/
│   └── argparse/                # 每个库一个子模块目录
├── data/
│   ├── libraries/libraries.json # 已收集的库（自动维护，可提交）
│   ├── catalog/recommended.json # 推荐库清单（手工编辑）
│   └── catalog/official.json    # 官方/标准扩展清单（手工编辑）
└── scripts/
    └── gui-interface.sh         # 启动脚本
```

> 整个仓库只有上面这些文件：一个 Python 服务、一个 HTML 页面、三份 JSON 数据和子模块。
> 没有 CMake 工程、没有需要编译的代码，`git clone` 完就能直接跑。

## REST 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | git 版本、仓库状态、已收集数量 |
| GET | `/api/libraries` | 已收集列表（叠加磁盘实时状态） |
| GET | `/api/catalog` | 推荐库清单 |
| POST | `/api/collect` | 收集一个库，body: `{url, name?, branch?, category?, description?, tags?}` |
| POST | `/api/discover` | 多源自动筛选，body: `{keywords?, topics?, sources[], minStars, updatedWithinDays, limit, maxSizeKb}` |
| POST | `/api/libraries/{name}/update` | 更新到远端最新 |
| DELETE | `/api/libraries/{name}` | 移除（deinit + rm + 删目录） |

## 自动探测的元信息

收集完成后会离线分析该库，不需要联网调用 GitHub API：

- **分支 / commit** —— 从工作区读取
- **许可证** —— 扫描 `LICENSE` / `COPYING` 文本特征
- **完整度（0-100）** —— README / LICENSE / 构建脚本 / CI / 测试目录，各 20 分
- **header-only** —— 是否存在 `single_include/` 或纯头文件 `include/`

## 安全约束

- 只接受 `https://`、`http://`、`ssh://`、`git@host:repo`，拒绝 `file://`
- 库名限制为 `[A-Za-z0-9._-]`，且校验落盘路径必须在 `external/` 内
- 全程用参数列表调用 git，不经过 shell，不存在命令注入
- 服务默认只监听 `127.0.0.1`

## 已知问题

- **GitHub 搜索接口未认证时限流很紧**（60 次/小时），一次筛选会消耗 1 次；带 `GITHUB_TOKEN` 可到 30 次/分钟。超额时页面会提示 `rateLimitRemaining`。
- **每次只取回 star 最高的前 N 条**（默认 30，可在界面调到 100）。GitHub 的 `total_count` 会如实显示，所以"共 108 条 / 取回 30 条 / 通过 15 条"三个数字都能看到，不会误以为库里只有 30 个。
- **vcpkg / Conan / 官方清单不提供 star 和活跃度**，这类条目会被标成「数据不足」，评分天然偏低，需要手动勾选收集。
- **许可证是展示项**：GitHub 上本身 `license` 为空的仓库会显示「许可证未知」，这是数据缺失，不代表不可用。

## 许可协议

MIT
