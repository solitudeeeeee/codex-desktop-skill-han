# Codex 桌面端 Skill 汉化

一个用于汉化 Codex Desktop Skill 菜单的本地插件。

它会扫描个人 Skill 和已启用插件中的 Skill，为 `/` 菜单写入中文名称和简短用途，并按需生成中文使用说明。原始 Skill 正文、代码和资源默认保持不变。

## 功能

- 扫描 `$CODEX_HOME/skills` 和当前已启用插件中的 Skill。
- 使用当前 Codex 模型生成中文菜单译文，结果持久缓存。
- 普通 Skill 只更新 `agents/openai.yaml`。
- ECC 等 `migrated-command-skills` 会额外更新 `SKILL.md` 的中文 `name` 和 `description`，因为这类菜单直接读取 frontmatter。
- 修改前自动备份，支持按 Skill 或全部恢复。
- 重复运行具备幂等性；遇到未知文件改动时拒绝覆盖。
- 中文使用说明按需生成，不预先翻译所有英文正文。

## 环境要求

- Codex Desktop
- Python 3.10 或更高版本

插件不保存 API Key，也不包含独立翻译 API。翻译批次通过当前已配置的 Codex 模型执行。

## 从 GitHub 安装

GitHub 仓库：[solitudeeeeee/codex-desktop-skill-han](https://github.com/solitudeeeeee/codex-desktop-skill-han)

GitHub 不支持中文仓库 slug，因此仓库地址使用 `codex-desktop-skill-han`；项目名称仍为“Codex 桌面端 Skill 汉化”。

```bash
codex plugin marketplace add solitudeeeeee/codex-desktop-skill-han
codex plugin add skill-han@codex-desktop-skill-han
```

安装后重新启动 Codex，并新建一个对话。

## 使用

首次汉化或新增、升级 Skill 后：

```text
使用 $skill-han 扫描并汉化新增或更新的 Skill。
```

查看某个 Skill 的中文用法：

```text
使用 $skill-han 解释 pdf。
```

恢复单个 Skill 或全部汉化：

```text
使用 $skill-han 恢复 pdf。
使用 $skill-han 恢复全部汉化。
```

## 数据位置

插件运行数据保存在：

```text
$CODEX_HOME/skill-han/
```

其中包括译文缓存、备份、修改清单和使用说明。状态检查命令：

```bash
python scripts/skill_han.py status --json
```

## 本地开发

运行测试：

```bash
python -m unittest discover -s tests -v
```

扫描当前可见 Skill：

```bash
python scripts/skill_han.py scan --json
```

应用已有译文：

```bash
python scripts/skill_han.py apply
```

## 边界

- 默认不翻译完整英文 `SKILL.md` 正文。
- 不修改 Skill 的代码和资源文件。
- ECC 迁移命令只替换 frontmatter 中的 `name` 与 `description`。
- 不自动合并冲突，不静默覆盖无法识别的后续改动。
