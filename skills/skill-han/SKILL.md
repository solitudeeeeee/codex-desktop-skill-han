---
name: skill-han
description: "汉化已安装 Codex Skill 的菜单名称与简短用途，并按需生成中文使用说明。用于扫描个人或插件 Skill、补译新增条目、查看中文用法或恢复原始菜单。"
---

# Skill 汉化

使用插件根目录的 `scripts/skill_han.py` 完成任务。先读取
[references/translation-policy.md](references/translation-policy.md) 后再翻译或生成说明。

## 汉化菜单

用户要求“汉化 Skill”“扫描新 Skill”或只调用 `$skill-han` 时：

1. 运行扫描和状态检查，报告当前可见、已汉化和待汉化数量。
2. 运行批量翻译。脚本使用当前 Codex 模型生成缺失译文，保存到持久缓存，再安全写入各 Skill 的
   `agents/openai.yaml`。不要手工修改目标 `SKILL.md`。
3. 遇到冲突、模型输出不完整或 YAML 校验失败时停止该项，说明具体 Skill；不要用 `--force` 覆盖未知改动。
4. 完成后报告已应用、已复用、待处理、冲突和说明缓存数量。

常用命令：

```text
python ../../scripts/skill_han.py translate --apply --all
python ../../scripts/skill_han.py status --json
```

如果当前会话无法等待全量翻译，采用可恢复的分批方式：

```text
python ../../scripts/skill_han.py translate --apply --limit 25
```

## 解释某个 Skill

用户问某个 Skill 怎么用时：

1. 运行 `lookup --json <关键词>` 找到唯一目标；有歧义时列出候选让用户选择。
2. 若已缓存说明且 `SKILL.md` 哈希未变化，直接展示缓存。
3. 否则读取目标的 `SKILL.md`，只依据正文和 frontmatter 生成中文说明，并按规则保存。
4. 说明必须让不懂英文的用户可以直接照着使用，而不是逐段翻译英文。

```text
python ../../scripts/skill_han.py lookup --json pdf
python ../../scripts/skill_han.py guide-save --key <KEY> --content-file <FILE>
```

## 恢复

用户明确要求恢复时，按目标执行；默认拒绝覆盖无法识别的后续改动。只有用户明确要求强制恢复时才使用
`--force`。

```text
python ../../scripts/skill_han.py restore --skill <KEY>
python ../../scripts/skill_han.py restore --all
```

## 边界

- 普通 Skill 只写 `agents/openai.yaml`。
- ECC 等包含 `.codex-plugin/migrated-command-skills` 的迁移命令，因为菜单直接读取
  `SKILL.md` frontmatter，额外替换该文件的 `name` 和 `description`；保留正文、代码和资源，
  并确保中文名称唯一。
- 不修改插件清单或 marketplace。
- 不改写命令、代码、URL、库名、schema 字段和专有名词。
- 不把英文执行逻辑全文翻译成中文；说明是供用户阅读的摘要和操作指南。
