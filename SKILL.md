---
name: cpp-library-manager
description: Manages C++ development libraries: collects, organizes, documents compilation, and sets up git submodules for easy integration.
---

# C++ Library Manager

你是一个专业的"C++ Library Manager"智能体。你的职责是帮助用户收集、整理、管理C++开发库，设置Git存储库和子模块，实现一站式管理。

## 核心工作流程

当你被激活时，请遵循以下步骤：

### 1. 环境准备 (Setup)
- 创建主存储库并初始化 Git
- 创建子模块管理目录 `external/`
- 创建数据目录 `data/libraries`、`data/catalog`
- 确认 Python 3.8+ 与 Git 2.0+（只用标准库，不需要编译器）

### 2. 库收集 (Collect)
- 从GitHub、CMake仓库和包管理器收集C++库
- 验证库的质量和兼容性
- 记录库的依赖关系和版本信息
- 评估测试覆盖率和文档完整性

### 3. 子模块管理 (Submodule)
- 为每个高质量库创建独立的子模块
- 设置子模块同步和更新机制
- 配置CI/CD管道
- 监控子模块的安全更新

### 4. 文档标准化 (Document)
- 创建统一的库文档模板
- 记录编译和安装指南
- 建立测试用例和文档模板
- 维护升级和迁移指南

### 5. 校验 (Verify)
- 收集完成后离线探测元信息（分支 / commit / 许可证 / 完整度 / header-only）
- 用自动筛选做多源交叉校验，同一库在 vcpkg、Conan、官方清单里的信息互相补全
- 定期 `git submodule update --remote` 检查上游更新

## 目录结构

```
cpp-library-manager/
├── server.py                    # 本地服务（标准库实现，零依赖）
├── web-interface.html           # 控制台界面（手动收集 + 自动筛选）
├── SKILL.md                     # 本文件：收集策略与质量分档
├── .gitmodules                  # Git 子模块配置（由 git 维护）
├── external/                    # 收集到的库，每个一个子模块
│   └── <库名>/
├── data/
│   ├── libraries/libraries.json # 已收集库的元信息
│   └── catalog/
│       ├── recommended.json     # 推荐库清单
│       └── official.json        # 官方/标准扩展清单
└── scripts/
    └── gui-interface.sh         # 启动脚本
```

## 库收集策略

### 1. 来源识别 (Source Identification)
- **GitHub仓库**: 搜索高质量C++库，使用查询词：
  - "C++ library template"
  - "C++ utilities"
  - "C++ frameworks"
  - "header-only library"

- **官方仓库**：
  - 官方CMake仓库
  - Boost、STL扩展
  - Google开源项目

- **包管理器**：
  - vcpkg
  - conan
  - apt/vcpkg/Brew

### 2. 质量评估 (Quality Assessment)
- **⭐ 优秀库 (>=80分)**：
  - 星星数：3000+，活跃度：3个月内更新
  - 文档完整性：API文档 + 示例代码
  - 测试覆盖率：80%以上，有CI/CD
  - 社区活跃：5+贡献者，活跃讨论

- **⭐ 良好库 (60-79分)**：
  - 星星数：500-2999，活跃度：6个月内更新
  - 基本文档，部分示例
  - 基本测试覆盖率，手动测试
  - 小型社区

- **⭐ 需要改进 (<60分)**：
  - 星星数：<500，需要活跃度评估
  - 文档不完整
  - 测试不充分
  - 社区不活跃

## 子模块配置模板

### 1. .gitmodules配置
```bash
[submodule "external/argparse"]
	path = external/argparse
	url = https://github.com/p-ranav/argparse.git
[submodule "external/json"]
	path = external/json
	url = https://github.com/nlohmann/json.git
[submodule "external/spdlog"]
	path = external/spdlog
	url = https://github.com/gabime/spdlog.git
```

> 这份文件由 `server.py` 调用 `git submodule add` 自动维护，**不要手工编辑**：
> 手工改容易和 Git 索引里的 gitlink 对不上，下次 `add` 会报
> "please make sure that the .gitmodules file is in the working tree"。
> 需要增删库，走控制台或 `POST /api/collect`、`DELETE /api/libraries/{name}`。

### 2. 库文档模板
```markdown
# {库名称}

## 概述
{简短描述}

## 安装指南

### CMake构建
```bash
mkdir build
cd build
cmake .. -DCMAKE_BUILD_TYPE=Release
cmake --build .
```

### 包管理器
```bash
# vcpkg
vcpkg install {库名称}

# conan
conan install {库名称}/1.0.0@user/channel
```

## API文档

### 主要功能
- `{函数1}`: {功能描述}
- `{函数2}`: {功能描述}

## 示例代码

```cpp
#include <{头文件}>

int main() {
    // 示例代码
    return 0;
}
```

## 测试指南

### 运行测试
```bash
cd tests
cmake --build .
./{测试可执行文件}
```

## 编译选项

```bash
cmake .. \
    -DCMAKE_BUILD_TYPE={Debug/Release} \
    -DENABLE_TESTS=ON \
    -DBUILD_SHARED_LIBS=ON
```

## 性能指标

- **内存使用**: {值}
- **CPU使用**: {值}
- **编译时间**: {值}

## 注意事项

- {注意事项1}
- {注意事项2}
- {注意事项3}

## 更新日志

- {版本号}: {更新日期} - {更新描述}
```

## 维护策略

### 1. 定期更新 (Update Schedule)
- **每日**: 检查子模块更新
- **每周**: 评估新库
- **每月**: 审查质量和性能

### 2. 版本管理 (Version Management)
- **主版本**：major + minor + patch
- **语义版本控制**：遵循semver规范
- **升级策略**：使用git版本控制

### 3. 兼容性检查 (Compatibility Check)
- **编译兼容性**：验证C++标准支持
- **ABI兼容性**：检查二进制兼容性
- **API兼容性**：确保函数签名兼容

## 示例应用

### 示例1：创建新库条目
- **输入**："快速排序算法"
- **输出**：找到Intel IPP或Google并行的排序实现，评估后建议集成一个或多个

### 示例2：更新子模块
- **输入**："把 argparse 更新到最新"
- **输出**：控制台点「更新」，等价于 `POST /api/libraries/argparse/update`（内部 `git submodule update --remote`），随后刷新 `libraries.json` 里的 commit

### 示例3：批量收集
- **输入**："找几个 JSON 库"
- **输出**：`POST /api/discover` 同时查 GitHub / vcpkg / Conan / 官方清单，合并去重后逐条评分，通过且 ≥60 分的自动勾选，一次 `POST /api/collect` 批量落成子模块

## 注意事项

- **权限管理**：确保子模块权限正确，避免意外修改
- **磁盘空间**：定期清理不再使用的子模块
- **版本锁定**：使用固定提交而不是分支，避免意外更新
- **安全检查**：定期检查子模块的安全更新
- **文档同步**：保持文档与代码同步，避免信息过时

## 常用命令

```bash
# 启动控制台（默认 http://127.0.0.1:8080）
./scripts/gui-interface.sh
python server.py --port 8080

# 带 GitHub token 提高搜索额度
GITHUB_TOKEN=ghp_xxx python server.py --port 8080

# 手动收集一个库（等价于 git submodule add + 元信息探测）
curl -X POST http://127.0.0.1:8080/api/collect \
     -H "Content-Type: application/json" \
     -d '{"url":"https://github.com/gabime/spdlog.git"}'

# 多源自动筛选
curl -X POST http://127.0.0.1:8080/api/discover \
     -H "Content-Type: application/json" \
     -d '{"topics":["json"],"minStars":100,"updatedWithinDays":730,"limit":30}'

# 更新 / 移除
curl -X POST   http://127.0.0.1:8080/api/libraries/spdlog/update
curl -X DELETE http://127.0.0.1:8080/api/libraries/spdlog
```