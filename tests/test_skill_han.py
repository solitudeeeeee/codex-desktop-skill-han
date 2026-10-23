from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

import skill_han  # noqa: E402


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def create_skill(root: Path, relative: str, name: str, description: str) -> Path:
    skill_file = root / relative / "SKILL.md"
    write(
        skill_file,
        "---\n"
        f"name: {name}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "---\n\n# Demo\n",
    )
    return skill_file


class YamlPatchTests(unittest.TestCase):
    def test_preserves_other_interface_fields_and_policy(self) -> None:
        original = (
            "interface:\n"
            '  display_name: "Old Name"\n'
            '  short_description: "Old description"\n'
            "  icon_small: \"./assets/icon.png\"\n"
            "\n"
            "policy:\n"
            "  allow_implicit_invocation: false\n"
        )
        patched = skill_han.patch_interface(original, "新名称（demo）", "处理演示文件并总结关键结果")
        fields = skill_han.interface_fields(patched)
        self.assertEqual(fields["display_name"], "新名称（demo）")
        self.assertIn("icon_small", patched)
        self.assertIn("allow_implicit_invocation: false", patched)

    def test_creates_interface_when_missing(self) -> None:
        original = "policy:\n  allow_implicit_invocation: true\n"
        patched = skill_han.patch_interface(original, "新名称（demo）", "处理演示文件并总结关键结果")
        self.assertTrue(patched.startswith("interface:\n"))
        self.assertIn("policy:", patched)

    def test_patches_only_frontmatter_description(self) -> None:
        original = (
            "---\n"
            'name: "source-command-demo"\n'
            'description: "English description."\n'
            "---\n\n"
            "# Body\n\n"
            "Keep this body unchanged.\n"
        )
        patched = skill_han.patch_frontmatter_description(original, "中文用途说明")
        self.assertIn('name: "source-command-demo"', patched)
        self.assertIn('description: "中文用途说明"', patched)
        self.assertIn("Keep this body unchanged.", patched)
        self.assertNotIn("English description.", patched)

    def test_patches_name_and_description_without_body_changes(self) -> None:
        original = (
            "---\n"
            'name: "source-command-demo"\n'
            'description: "English description."\n'
            "---\n\n# Body\n"
        )
        patched = skill_han.patch_frontmatter_fields(
            original,
            {"name": "演示命令", "description": "中文用途说明"},
        )
        self.assertIn('name: "演示命令"', patched)
        self.assertIn('description: "中文用途说明"', patched)
        self.assertTrue(patched.endswith("# Body\n"))


class DiscoveryTests(unittest.TestCase):
    def test_scans_personal_and_enabled_plugin_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            create_skill(home / "skills", "personal-demo", "personal-demo", "Personal demo skill.")
            write(
                home / "config.toml",
                '[plugins."demo@market"]\nenabled = true\n\n'
                '[plugins."disabled@market"]\nenabled = false\n',
            )
            cache = home / "plugins" / "cache" / "market" / "demo"
            create_skill(cache / "1.0.0" / "skills", "old", "old-skill", "Old version.")
            create_skill(cache / "1.1.0" / "skills", "new", "new-skill", "New version.")
            create_skill(
                home / "plugins" / "cache" / "market" / "disabled" / "1.0.0" / "skills",
                "off",
                "off-skill",
                "Disabled.",
            )
            records = skill_han.discover_skills(home)
            self.assertEqual([record.skill_name for record in records], ["personal-demo", "new-skill"])
            self.assertEqual(records[1].version, "1.1.0")

    def test_scans_migrated_command_skills(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            write(home / "config.toml", '[plugins."demo@market"]\nenabled = true\n')
            root = home / "plugins" / "cache" / "market" / "demo" / "1.0.0"
            create_skill(root / "skills", "regular", "regular", "Regular skill.")
            create_skill(
                root / ".codex-plugin" / "migrated-command-skills",
                "review-pr",
                "source-command-review-pr",
                "Review a pull request.",
            )
            records = skill_han.discover_skills(home)
            self.assertEqual(len(records), 2)
            migrated = next(record for record in records if "migrated-command" in record.key)
            self.assertEqual(
                migrated.key,
                "plugin:market/demo:migrated-command/review-pr",
            )


class ApplyRestoreTests(unittest.TestCase):
    def test_apply_is_idempotent_and_restore_is_exact(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            skill_file = create_skill(
                home / "skills",
                "demo",
                "demo",
                "Create a demo artifact from input files.",
            )
            original_skill_hash = skill_han.file_hash(skill_file)
            original_yaml = (
                "interface:\n"
                '  display_name: "Demo"\n'
                '  short_description: "Create demo artifacts"\n'
                "policy:\n"
                "  allow_implicit_invocation: true\n"
            )
            yaml_path = skill_file.parent / "agents" / "openai.yaml"
            write(yaml_path, original_yaml)
            record = skill_han.discover_skills(home)[0]
            translations = skill_han.empty_translations()
            translations["entries"][record.key] = {
                "skill_name": "demo",
                "source_hash": record.source_hash,
                "display_name": "演示制作（demo）",
                "short_description": "读取输入文件并生成可直接使用的演示产物",
                "generated_at": skill_han.utc_now(),
            }
            skill_han.save_json(skill_han.translations_path(home), translations)

            first = skill_han.apply_translations(home=home)
            self.assertEqual(first["applied"], 1)
            self.assertEqual(skill_han.file_hash(skill_file), original_skill_hash)
            self.assertIn("演示制作（demo）", yaml_path.read_text(encoding="utf-8"))
            second = skill_han.apply_translations(home=home)
            self.assertEqual(second["applied"], 0)
            self.assertEqual(second["unchanged"], 1)

            write(
                yaml_path,
                yaml_path.read_text(encoding="utf-8").replace(
                    "演示制作（demo）",
                    "用户改过（demo）",
                ),
            )
            conflict = skill_han.apply_translations(home=home)
            self.assertEqual(len(conflict["conflicts"]), 1)

            restored = skill_han.restore_targets(
                home=home,
                skill_key=record.key,
                restore_all=False,
                force=True,
            )
            self.assertEqual(restored["restored"], 1)
            self.assertEqual(yaml_path.read_text(encoding="utf-8"), original_yaml)

    def test_restore_removes_file_that_did_not_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            skill_file = create_skill(home / "skills", "demo", "demo", "Create demo output.")
            record = skill_han.discover_skills(home)[0]
            translations = skill_han.empty_translations()
            translations["entries"][record.key] = {
                "skill_name": "demo",
                "source_hash": record.source_hash,
                "display_name": "演示制作（demo）",
                "short_description": "读取输入内容并生成完整可用的演示结果",
                "generated_at": skill_han.utc_now(),
            }
            skill_han.save_json(skill_han.translations_path(home), translations)
            skill_han.apply_translations(home=home)
            yaml_path = skill_file.parent / "agents" / "openai.yaml"
            self.assertTrue(yaml_path.exists())
            skill_han.restore_targets(
                home=home,
                skill_key=record.key,
                restore_all=False,
                force=False,
            )
            self.assertFalse(yaml_path.exists())

    def test_migrated_command_updates_and_restores_both_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            write(home / "config.toml", '[plugins."demo@market"]\nenabled = true\n')
            migrated_root = (
                home
                / "plugins"
                / "cache"
                / "market"
                / "demo"
                / "1.0.0"
                / ".codex-plugin"
                / "migrated-command-skills"
            )
            skill_file = create_skill(
                migrated_root,
                "review-pr",
                "source-command-review-pr",
                "English review description.",
            )
            original_skill = skill_file.read_text(encoding="utf-8")
            yaml_path = skill_file.parent / "agents" / "openai.yaml"
            original_yaml = (
                "interface:\n"
                '  display_name: "Review PR"\n'
                '  short_description: "English review description"\n'
            )
            write(yaml_path, original_yaml)
            record = skill_han.discover_skills(home)[0]
            translations = skill_han.empty_translations()
            translations["entries"][record.key] = {
                "skill_name": record.skill_name,
                "source_hash": record.source_hash,
                "display_name": "审查 PR（source-command-review-pr）",
                "short_description": "调用多个专用 agents 审查 PR 并汇总风险",
                "generated_at": skill_han.utc_now(),
            }
            skill_han.save_json(skill_han.translations_path(home), translations)

            first = skill_han.apply_translations(home=home)
            self.assertEqual(first["applied"], 1)
            self.assertIn('name: "审查 PR"', skill_file.read_text(encoding="utf-8"))
            self.assertIn("调用多个专用 agents", skill_file.read_text(encoding="utf-8"))
            self.assertIn("调用多个专用 agents", yaml_path.read_text(encoding="utf-8"))
            self.assertEqual(skill_han.status_report(home)["pending"], 0)

            second = skill_han.apply_translations(home=home)
            self.assertEqual(second["applied"], 0)
            self.assertEqual(second["unchanged"], 1)

            skill_han.restore_targets(
                home=home,
                skill_key=record.key,
                restore_all=False,
                force=False,
            )
            self.assertEqual(skill_file.read_text(encoding="utf-8"), original_skill)
            self.assertEqual(yaml_path.read_text(encoding="utf-8"), original_yaml)


class TranslationValidationTests(unittest.TestCase):
    def test_rejects_missing_identifier_and_overlong_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            create_skill(home / "skills", "demo", "demo", "Create demo output.")
            record = skill_han.discover_skills(home)[0]
            errors = skill_han.validate_translation(
                record.key,
                "演示制作",
                "这是一段刻意写得很长很长的说明" * 10,
                {record.key: record},
            )
            self.assertGreaterEqual(len(errors), 2)


if __name__ == "__main__":
    unittest.main()
