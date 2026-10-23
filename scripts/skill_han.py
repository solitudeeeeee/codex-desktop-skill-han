#!/usr/bin/env python3
"""Localize Codex skill menus without modifying SKILL.md files."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = 1
SELF_PLUGIN_ID = "skill-han@personal"
CHINESE_RE = re.compile(r"[\u3400-\u9fff]")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser().resolve()


def state_root(home: Path | None = None) -> Path:
    return (home or codex_home()) / "skill-han"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mode = path.stat().st_mode if path.exists() else None
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temp_name, path)
        if previous_mode is not None:
            os.chmod(path, previous_mode)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_write_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(value)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取 JSON：{path}: {exc}") from exc


def save_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def translations_path(home: Path | None = None) -> Path:
    return state_root(home) / "translations.json"


def manifest_path(home: Path | None = None) -> Path:
    return state_root(home) / "manifest.json"


def empty_translations() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "entries": {}}


def empty_manifest() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "targets": {}}


def load_translations(home: Path | None = None) -> dict[str, Any]:
    data = load_json(translations_path(home), empty_translations())
    if data.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("translations.json 版本不受支持")
    data.setdefault("entries", {})
    return data


def load_manifest(home: Path | None = None) -> dict[str, Any]:
    data = load_json(manifest_path(home), empty_manifest())
    if data.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("manifest.json 版本不受支持")
    data.setdefault("targets", {})
    return data


@dataclass
class SkillRecord:
    key: str
    skill_name: str
    description: str
    source_kind: str
    plugin_id: str
    marketplace: str
    plugin: str
    version: str
    relative_dir: str
    skill_dir: str
    skill_file: str
    yaml_path: str
    current_display_name: str
    current_short_description: str
    source_hash: str

    def public(self) -> dict[str, Any]:
        return asdict(self)


def split_frontmatter(text: str) -> list[str]:
    lines = text.splitlines()
    if lines and lines[0].lstrip("\ufeff").strip() == "---":
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return lines[1:index]
    return []


def unquote_yaml_scalar(value: str) -> str:
    value = value.strip()
    if not value or value in {"null", "~"}:
        return ""
    if value.startswith('"'):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value.strip('"')
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    return value


def frontmatter_fields(text: str) -> dict[str, str]:
    lines = split_frontmatter(text)
    fields: dict[str, str] = {}
    index = 0
    while index < len(lines):
        match = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", lines[index])
        if not match:
            index += 1
            continue
        key, raw = match.group(1), match.group(2).strip()
        if key not in {"name", "description"}:
            index += 1
            continue
        if raw in {">", ">-", ">+", "|", "|-", "|+"}:
            block: list[str] = []
            index += 1
            while index < len(lines):
                line = lines[index]
                if line and not line[0].isspace():
                    break
                block.append(line)
                index += 1
            indent = min(
                (len(line) - len(line.lstrip()) for line in block if line.strip()),
                default=0,
            )
            content = [line[indent:] if line.strip() else "" for line in block]
            separator = "\n" if raw.startswith("|") else " "
            fields[key] = separator.join(part.strip() for part in content if part.strip()).strip()
            continue
        fields[key] = unquote_yaml_scalar(raw)
        index += 1
    return fields


def patch_frontmatter_fields(text: str, values: dict[str, str]) -> str:
    eol = "\r\n" if "\r\n" in text else "\n"
    had_trailing_newline = text.endswith(("\n", "\r"))
    lines = text.splitlines()
    if not lines or lines[0].lstrip("\ufeff").strip() != "---":
        raise ValueError("SKILL.md 缺少 frontmatter")
    for key, value in values.items():
        if key not in {"name", "description"}:
            raise ValueError(f"不支持修改 frontmatter 字段：{key}")
        end = next(
            (index for index in range(1, len(lines)) if lines[index].strip() == "---"),
            None,
        )
        if end is None:
            raise ValueError("SKILL.md frontmatter 未闭合")
        field_index = next(
            (
                index
                for index in range(1, end)
                if re.match(rf"^{re.escape(key)}\s*:", lines[index])
            ),
            None,
        )
        if field_index is None:
            lines.insert(1, f"{key}: {yaml_quote(value)}")
        else:
            replacement_end = field_index + 1
            while replacement_end < end:
                candidate = lines[replacement_end]
                if candidate and not candidate[0].isspace():
                    break
                replacement_end += 1
            lines[field_index:replacement_end] = [f"{key}: {yaml_quote(value)}"]
    result = eol.join(lines)
    if had_trailing_newline:
        result += eol
    parsed = frontmatter_fields(result)
    for key, value in values.items():
        if parsed.get(key) != value:
            raise ValueError(f"{key} 写入后校验失败")
    return result


def patch_frontmatter_description(text: str, description: str) -> str:
    return patch_frontmatter_fields(text, {"description": description})


def interface_fields(text: str) -> dict[str, str]:
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if re.match(r"^interface\s*:", line)), None)
    if start is None:
        return {}
    values: dict[str, str] = {}
    index = start + 1
    while index < len(lines):
        line = lines[index]
        if line and not line[0].isspace() and not line.lstrip().startswith("#"):
            break
        match = re.match(r"^\s+(display_name|short_description)\s*:\s*(.*)$", line)
        if match:
            values[match.group(1)] = unquote_yaml_scalar(match.group(2))
        index += 1
    return values


def yaml_quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def patch_interface(
    original: str,
    display_name: str,
    short_description: str,
) -> str:
    lines = original.splitlines()
    if not lines:
        return (
            "interface:\n"
            f"  display_name: {yaml_quote(display_name)}\n"
            f"  short_description: {yaml_quote(short_description)}\n"
        )

    interface_index = next(
        (i for i, line in enumerate(lines) if re.match(r"^interface\s*:", line)),
        None,
    )
    if interface_index is None:
        lines[0:0] = [
            "interface:",
            f"  display_name: {yaml_quote(display_name)}",
            f"  short_description: {yaml_quote(short_description)}",
            "",
        ]
    else:
        raw_tail = lines[interface_index].split(":", 1)[1].strip()
        if raw_tail not in {"", "{}", "null", "~"}:
            raise ValueError("interface 不是可安全扩展的块格式")
        lines[interface_index] = "interface:"
        end = interface_index + 1
        while end < len(lines):
            line = lines[end]
            if line and not line[0].isspace() and not line.lstrip().startswith("#"):
                break
            end += 1
        block = lines[interface_index + 1 : end]
        found = {"display_name": False, "short_description": False}
        child_indent = ""
        for offset, line in enumerate(block):
            indent_match = re.match(r"^(\s+)\S", line)
            if indent_match and not child_indent:
                child_indent = indent_match.group(1)
            for field, value in (
                ("display_name", display_name),
                ("short_description", short_description),
            ):
                if re.match(rf"^\s+{field}\s*:", line):
                    block[offset] = f"{child_indent or '  '}{field}: {yaml_quote(value)}"
                    found[field] = True
        child_indent = child_indent or "  "
        missing = [
            f"{child_indent}{field}: {yaml_quote(value)}"
            for field, value in (
                ("display_name", display_name),
                ("short_description", short_description),
            )
            if not found[field]
        ]
        lines[interface_index + 1 : end] = missing + block

    result = "\n".join(lines).rstrip() + "\n"
    parsed = interface_fields(result)
    if parsed.get("display_name") != display_name:
        raise ValueError("display_name 写入后校验失败")
    if parsed.get("short_description") != short_description:
        raise ValueError("short_description 写入后校验失败")
    return result


def natural_key(value: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", value)]


def enabled_plugin_ids(home: Path) -> set[str]:
    config_path = home / "config.toml"
    if not config_path.exists():
        return set()
    text = config_path.read_text(encoding="utf-8-sig")
    enabled: set[str] = set()
    matches = list(re.finditer(r'^\[plugins\."([^"]+)"\]\s*$', text, flags=re.MULTILINE))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.end() : end]
        if re.search(r"^\s*enabled\s*=\s*true\s*$", block, flags=re.MULTILINE):
            enabled.add(match.group(1))
    return enabled


def skill_record(
    skill_file: Path,
    *,
    key: str,
    source_kind: str,
    plugin_id: str,
    marketplace: str,
    plugin: str,
    version: str,
    relative_dir: str,
) -> SkillRecord:
    text = read_text(skill_file)
    fields = frontmatter_fields(text)
    skill_name = fields.get("name") or skill_file.parent.name
    description = fields.get("description", "").strip()
    yaml_path = skill_file.parent / "agents" / "openai.yaml"
    current = interface_fields(read_text(yaml_path)) if yaml_path.exists() else {}
    source_hash = sha256_text(f"{skill_name}\0{description}")
    return SkillRecord(
        key=key,
        skill_name=skill_name,
        description=description,
        source_kind=source_kind,
        plugin_id=plugin_id,
        marketplace=marketplace,
        plugin=plugin,
        version=version,
        relative_dir=relative_dir,
        skill_dir=str(skill_file.parent.resolve()),
        skill_file=str(skill_file.resolve()),
        yaml_path=str(yaml_path.resolve()),
        current_display_name=current.get("display_name", ""),
        current_short_description=current.get("short_description", ""),
        source_hash=source_hash,
    )


def iter_personal_skills(home: Path) -> Iterable[SkillRecord]:
    root = home / "skills"
    if not root.exists():
        return
    for current_root, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(name for name in dirs if name not in {"node_modules", ".git"})
        if "SKILL.md" not in files:
            continue
        skill_file = Path(current_root) / "SKILL.md"
        relative = skill_file.parent.relative_to(root).as_posix()
        yield skill_record(
            skill_file,
            key=f"personal:{relative}",
            source_kind="personal",
            plugin_id="",
            marketplace="",
            plugin="",
            version="",
            relative_dir=relative,
        )


def iter_plugin_skills(home: Path) -> Iterable[SkillRecord]:
    cache_root = home / "plugins" / "cache"
    if not cache_root.exists():
        return
    enabled = enabled_plugin_ids(home)
    for marketplace_dir in sorted(cache_root.iterdir(), key=lambda p: p.name):
        if not marketplace_dir.is_dir():
            continue
        for plugin_dir in sorted(marketplace_dir.iterdir(), key=lambda p: p.name):
            if not plugin_dir.is_dir():
                continue
            plugin_id = f"{plugin_dir.name}@{marketplace_dir.name}"
            if plugin_id == SELF_PLUGIN_ID:
                continue
            if enabled and plugin_id not in enabled:
                continue
            versions = sorted(
                (entry for entry in plugin_dir.iterdir() if entry.is_dir()),
                key=lambda p: natural_key(p.name),
            )
            if not versions:
                versions = [plugin_dir]
            version_dir = versions[-1]
            skills_dir = version_dir / "skills"
            component_roots = [
                (skills_dir, ""),
                (version_dir / ".codex-plugin" / "migrated-command-skills", "migrated-command/"),
            ]
            for component_root, key_prefix in component_roots:
                if not component_root.exists():
                    continue
                definitions = sorted(
                    (entry for entry in component_root.iterdir() if entry.is_dir()),
                    key=lambda p: p.name,
                )
                for definition in definitions:
                    skill_file = definition / "SKILL.md"
                    if not skill_file.exists():
                        continue
                    relative = key_prefix + definition.relative_to(component_root).as_posix()
                    yield skill_record(
                        skill_file,
                        key=f"plugin:{marketplace_dir.name}/{plugin_dir.name}:{relative}",
                        source_kind="plugin",
                        plugin_id=plugin_id,
                        marketplace=marketplace_dir.name,
                        plugin=plugin_dir.name,
                        version=version_dir.name,
                        relative_dir=relative,
                    )


def discover_skills(home: Path | None = None) -> list[SkillRecord]:
    home = (home or codex_home()).resolve()
    records = list(iter_personal_skills(home)) + list(iter_plugin_skills(home))
    return sorted(records, key=lambda record: record.key)


def normalized_corpus(description: str) -> str:
    return re.sub(r"\s+", " ", description).strip()


def validate_translation(
    key: str,
    display_name: str,
    short_description: str,
    records: dict[str, SkillRecord],
) -> list[str]:
    errors: list[str] = []
    record = records[key]
    display_name = display_name.strip()
    short_description = normalized_corpus(short_description)
    if not display_name:
        errors.append("display_name 为空")
    if "\n" in display_name or "\n" in short_description:
        errors.append("翻译字段不能包含换行")
    directory_name = record.relative_dir.rsplit("/", 1)[-1]
    identifiers = {record.skill_name, directory_name}
    if ":migrated-command/" in record.key:
        identifiers.add(f"source-command-{directory_name}")
    if not any(
        f"（{identifier}）" in display_name or f"({identifier})" in display_name
        for identifier in identifiers
    ):
        errors.append("display_name 未保留原标识")
    if len(display_name) > 80:
        errors.append("display_name 超过 80 字符")
    if not 10 <= len(short_description) <= 64:
        errors.append("short_description 必须在 10–64 字符之间")
    if not CHINESE_RE.search(display_name + short_description):
        errors.append("译文缺少中文")
    return errors


def translation_is_current(
    record: SkillRecord,
    translations: dict[str, Any],
) -> bool:
    entry = translations.get("entries", {}).get(record.key)
    if not entry:
        return False
    source_matches = record.source_hash in {
        entry.get("source_hash"),
        entry.get("localized_source_hash"),
    }
    return source_matches and bool(entry.get("display_name")) and bool(
        entry.get("short_description")
    )


def pending_records(
    records: list[SkillRecord],
    translations: dict[str, Any],
) -> list[SkillRecord]:
    return [record for record in records if not translation_is_current(record, translations)]


def target_id(path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve()))
    return sha256_text(normalized)[:24]


def file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    return sha256_bytes(path.read_bytes())


def prepare_backup_for_path(
    *,
    skill_key: str,
    target: Path,
    manifest: dict[str, Any],
    home: Path,
    kind: str,
) -> tuple[str, dict[str, Any]]:
    identifier = target_id(target)
    targets = manifest["targets"]
    if identifier in targets:
        entry = targets[identifier]
        if Path(entry["target"]) != target.resolve():
            raise RuntimeError("备份目标路径发生变化")
        return identifier, entry

    backup_dir = home / "skill-han" / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    existed = target.exists()
    backup_file = backup_dir / f"{identifier}.{target.name}.backup"
    marker_file = backup_dir / f"{identifier}.missing.json"
    if existed:
        atomic_write_bytes(backup_file, target.read_bytes())
        if marker_file.exists():
            marker_file.unlink()
    else:
        atomic_write_text(
            marker_file,
            json.dumps({"target": str(target.resolve()), "created_at": utc_now()}, ensure_ascii=False)
            + "\n",
        )
        if backup_file.exists():
            backup_file.unlink()
    entry = {
        "skill_key": skill_key,
        "target": str(target.resolve()),
        "kind": kind,
        "existed": existed,
        "backup_file": str(backup_file) if existed else "",
        "marker_file": str(marker_file) if not existed else "",
        "original_sha256": file_hash(target),
        "applied_sha256": None,
        "updated_at": utc_now(),
    }
    targets[identifier] = entry
    return identifier, entry


def prepare_backup(
    record: SkillRecord,
    manifest: dict[str, Any],
    home: Path,
) -> tuple[str, dict[str, Any]]:
    return prepare_backup_for_path(
        skill_key=record.key,
        target=Path(record.yaml_path),
        manifest=manifest,
        home=home,
        kind="openai_yaml",
    )


def check_target_drift(target: Path, entry: dict[str, Any]) -> bool:
    current_hash = file_hash(target)
    allowed = {entry.get("original_sha256"), entry.get("applied_sha256")}
    return current_hash not in allowed


def localized_command_name(display_name: str, fallback: str) -> str:
    name = re.sub(r"\s*[（(][^（）()]+[）)]\s*$", "", display_name).strip()
    return name or fallback


def apply_translations(
    *,
    home: Path,
    dry_run: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    records = discover_skills(home)
    record_map = {record.key: record for record in records}
    translations = load_translations(home)
    manifest = load_manifest(home)
    applied: list[str] = []
    unchanged: list[str] = []
    pending: list[str] = []
    conflicts: list[dict[str, str]] = []
    translations_changed = False

    for record in records:
        if not translation_is_current(record, translations):
            pending.append(record.key)
            continue
        entry = translations["entries"][record.key]
        errors = validate_translation(
            record.key,
            entry["display_name"],
            entry["short_description"],
            record_map,
        )
        if errors:
            conflicts.append({"key": record.key, "reason": "；".join(errors)})
            continue
        yaml_target = Path(record.yaml_path)
        yaml_original = read_text(yaml_target) if yaml_target.exists() else ""
        try:
            yaml_updated = patch_interface(
                yaml_original,
                entry["display_name"],
                entry["short_description"],
            )
        except ValueError as exc:
            conflicts.append({"key": record.key, "reason": f"YAML 更新失败：{exc}"})
            continue
        changes: list[tuple[str, Path, str, str]] = []
        if yaml_updated != yaml_original:
            changes.append(("openai.yaml", yaml_target, yaml_original, yaml_updated))

        if ":migrated-command/" in record.key:
            skill_target = Path(record.skill_file)
            skill_original = read_text(skill_target)
            localized_name = localized_command_name(
                entry["display_name"],
                record.skill_name,
            )
            try:
                skill_updated = patch_frontmatter_fields(
                    skill_original,
                    {
                        "name": localized_name,
                        "description": entry["short_description"],
                    },
                )
            except ValueError as exc:
                conflicts.append({"key": record.key, "reason": f"SKILL.md 更新失败：{exc}"})
                continue
            if skill_updated != skill_original:
                changes.append(("SKILL.md", skill_target, skill_original, skill_updated))

        if not changes:
            unchanged.append(record.key)
            continue

        prepared: list[tuple[str, Path, str, str, str, dict[str, Any]]] = []
        conflict_found = False
        for kind, target, original, updated in changes:
            identifier, backup_entry = prepare_backup_for_path(
                skill_key=record.key,
                target=target,
                manifest=manifest,
                home=home,
                kind=kind,
            )
            drift = check_target_drift(target, backup_entry)
            if drift and not force:
                conflicts.append(
                    {
                        "key": record.key,
                        "reason": f"{kind} 含无法识别的后续改动",
                    }
                )
                conflict_found = True
                continue
            if drift and force:
                conflict_dir = home / "skill-han" / "conflicts"
                conflict_dir.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    snapshot = (
                        conflict_dir
                        / f"{identifier}.{utc_now().replace(':', '-')}.{kind}.backup"
                    )
                    atomic_write_bytes(snapshot, target.read_bytes())
            prepared.append((kind, target, original, updated, identifier, backup_entry))
        if conflict_found:
            continue

        if not dry_run:
            for kind, target, _original, updated, _identifier, backup_entry in prepared:
                atomic_write_text(target, updated)
                backup_entry["applied_sha256"] = file_hash(target)
                backup_entry["updated_at"] = utc_now()
                if kind == "SKILL.md":
                    entry["localized_name"] = localized_name
                    entry["localized_source_hash"] = sha256_text(
                        f"{localized_name}\0{entry['short_description']}"
                    )
                    translations_changed = True
        applied.append(record.key)

    if not dry_run:
        manifest["updated_at"] = utc_now()
        save_json(manifest_path(home), manifest)
        if translations_changed:
            translations["updated_at"] = utc_now()
            save_json(translations_path(home), translations)
    return {
        "total": len(records),
        "applied": len(applied),
        "unchanged": len(unchanged),
        "pending": len(pending),
        "conflicts": conflicts,
        "applied_keys": applied,
        "pending_keys": pending,
    }


def status_report(home: Path) -> dict[str, Any]:
    records = discover_skills(home)
    translations = load_translations(home)
    manifest = load_manifest(home)
    current = [record for record in records if translation_is_current(record, translations)]
    pending = len(records) - len(current)
    guides_dir = home / "skill-han" / "guides"
    guides = len(list(guides_dir.glob("*.md"))) if guides_dir.exists() else 0
    conflicts: list[str] = []
    for entry in manifest["targets"].values():
        target = Path(entry["target"])
        if check_target_drift(target, entry):
            conflicts.append(entry["skill_key"])
    by_source: dict[str, int] = {}
    for record in records:
        by_source[record.source_kind] = by_source.get(record.source_kind, 0) + 1
    return {
        "total": len(records),
        "translated": len(current),
        "pending": pending,
        "guides": guides,
        "conflicts": sorted(set(conflicts)),
        "by_source": by_source,
        "state_dir": str(home / "skill-han"),
    }


def find_codex_cli() -> str:
    candidates: list[Path] = []
    env_value = os.environ.get("CODEX_CLI_PATH")
    if env_value:
        candidates.append(Path(env_value))
    which = shutil.which("codex")
    if which:
        candidates.append(Path(which))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.extend(
            sorted(
                (Path(local_app_data) / "OpenAI" / "Codex" / "bin").glob("*/codex.exe"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    raise RuntimeError("找不到 codex.exe；请设置 CODEX_CLI_PATH")


def translation_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["translations"],
        "properties": {
            "translations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["key", "display_name", "short_description"],
                    "properties": {
                        "key": {"type": "string"},
                        "display_name": {"type": "string"},
                        "short_description": {"type": "string"},
                    },
                },
            }
        },
    }


def translation_prompt(batch: list[dict[str, str]]) -> str:
    payload = json.dumps(batch, ensure_ascii=False, indent=2)
    return f"""只输出结构化 JSON，不要调用工具、不要读取文件、不要使用任何 Skill。
你是 Codex Skill 菜单的中文本地化翻译器。把下列条目翻译成简体中文。

规则：
1. display_name 固定为“中文功能名（原 skill_name）”，保留原标识。
2. short_description 用 20-40 个中文字符说明“能做什么 + 典型场景”，总长度不超过 64。
3. 保留库名、命令、协议、格式、schema 字段和专有名词的英文。
4. 结果必须覆盖输入中的每个 key，key 原样返回。
5. 不要写解释、Markdown 或代码围栏。

输入：
{payload}
"""


def parse_translation_output(text: str) -> dict[str, dict[str, str]]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    data = json.loads(stripped)
    values = data.get("translations")
    if not isinstance(values, list):
        raise ValueError("模型输出缺少 translations 数组")
    result: dict[str, dict[str, str]] = {}
    for item in values:
        if not isinstance(item, dict):
            raise ValueError("translations 条目不是对象")
        key = str(item.get("key", ""))
        result[key] = {
            "display_name": str(item.get("display_name", "")).strip(),
            "short_description": normalized_corpus(str(item.get("short_description", ""))),
        }
    return result


def run_translation_batch(batch: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    codex = find_codex_cli()
    work_root = state_root() / "work"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="translate-", dir=work_root) as temp_dir:
        temp = Path(temp_dir)
        schema_path = temp / "schema.json"
        output_path = temp / "output.json"
        save_json(schema_path, translation_schema())
        command = [
            codex,
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--color",
            "never",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
            "-C",
            str(temp),
            "-",
        ]
        completed = subprocess.run(
            command,
            input=translation_prompt(batch),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=420,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(f"Codex 翻译批次失败（{completed.returncode}）：{detail[-2000:]}")
        if not output_path.exists():
            raise RuntimeError("Codex 没有生成最终消息")
        return parse_translation_output(read_text(output_path))


def translate_pending(
    *,
    home: Path,
    batch_size: int,
    limit: int | None,
    apply: bool,
    dry_run_apply: bool,
) -> dict[str, Any]:
    records = discover_skills(home)
    record_map = {record.key: record for record in records}
    translations = load_translations(home)
    pending = pending_records(records, translations)
    if limit is not None:
        pending = pending[:limit]
    generated = 0
    rejected: list[dict[str, Any]] = []
    for offset in range(0, len(pending), batch_size):
        chunk = pending[offset : offset + batch_size]
        payload = [
            {
                "key": record.key,
                "skill_name": record.skill_name,
                "description": record.description[:4000],
                "current_display_name": record.current_display_name,
                "current_short_description": record.current_short_description,
                "source": record.plugin_id or "personal",
            }
            for record in chunk
        ]
        print(
            f"[skill-han] 翻译 {offset + 1}-{offset + len(chunk)} / {len(pending)}",
            flush=True,
        )
        output = run_translation_batch(payload)
        for record in chunk:
            item = output.get(record.key)
            if not item:
                rejected.append({"key": record.key, "reason": "模型未返回该 key"})
                continue
            errors = validate_translation(
                record.key,
                item["display_name"],
                item["short_description"],
                record_map,
            )
            if errors:
                rejected.append({"key": record.key, "reason": "；".join(errors)})
                continue
            translations["entries"][record.key] = {
                "skill_name": record.skill_name,
                "source_hash": record.source_hash,
                "display_name": item["display_name"],
                "short_description": item["short_description"],
                "generated_at": utc_now(),
            }
            generated += 1
        translations["updated_at"] = utc_now()
        save_json(translations_path(home), translations)
    apply_report = None
    if apply:
        apply_report = apply_translations(home=home, dry_run=dry_run_apply)
    return {
        "scheduled": len(pending),
        "generated": generated,
        "rejected": rejected,
        "remaining": len(pending_records(discover_skills(home), load_translations(home))),
        "apply": apply_report,
    }


def lookup_records(home: Path, query: str) -> list[SkillRecord]:
    records = discover_skills(home)
    exact = [record for record in records if record.key == query]
    if exact:
        return exact
    lowered = query.casefold()
    named = [
        record
        for record in records
        if record.skill_name.casefold() == lowered
        or record.current_display_name.casefold() == lowered
    ]
    if named:
        return named
    return [
        record
        for record in records
        if lowered in record.key.casefold()
        or lowered in record.skill_name.casefold()
        or lowered in record.current_display_name.casefold()
        or lowered in record.description.casefold()
    ]


def guide_path_for(home: Path, key: str) -> Path:
    return home / "skill-han" / "guides" / f"{sha256_text(key)[:24]}.md"


def guide_metadata_path(home: Path, key: str) -> Path:
    return home / "skill-han" / "guides" / f"{sha256_text(key)[:24]}.json"


def guide_status(home: Path, record: SkillRecord) -> dict[str, Any]:
    metadata = load_json(guide_metadata_path(home, record.key), {})
    current = metadata.get("source_hash") == record.source_hash
    return {
        "cached": guide_path_for(home, record.key).exists() and current,
        "path": str(guide_path_for(home, record.key)),
        "source_hash": record.source_hash,
        "metadata": metadata if metadata else None,
    }


def restore_targets(
    *,
    home: Path,
    skill_key: str | None,
    restore_all: bool,
    force: bool,
) -> dict[str, Any]:
    manifest = load_manifest(home)
    targets = manifest["targets"]
    selected: list[tuple[str, dict[str, Any]]] = []
    if restore_all:
        selected = list(targets.items())
    else:
        candidate_matches = [
            (identifier, entry)
            for identifier, entry in targets.items()
            if entry["skill_key"] == skill_key or skill_key in entry["skill_key"]
        ]
        if not candidate_matches:
            raise RuntimeError("没有找到匹配的已汉化目标")
        matched_keys = {entry["skill_key"] for _, entry in candidate_matches}
        if len(matched_keys) > 1:
            raise RuntimeError("匹配到多个目标，请提供更完整的 KEY")
        selected = candidate_matches
    restored: list[str] = []
    conflicts: list[dict[str, str]] = []
    for identifier, entry in selected:
        target = Path(entry["target"])
        if check_target_drift(target, entry) and not force:
            conflicts.append({"key": entry["skill_key"], "reason": "目标含后续改动，拒绝覆盖"})
            continue
        if entry["existed"]:
            backup = Path(entry["backup_file"])
            if not backup.exists():
                conflicts.append({"key": entry["skill_key"], "reason": "备份文件缺失"})
                continue
            atomic_write_bytes(target, backup.read_bytes())
        elif target.exists():
            target.unlink()
        targets.pop(identifier, None)
        restored.append(entry["skill_key"])
    manifest["updated_at"] = utc_now()
    save_json(manifest_path(home), manifest)
    return {"restored": len(restored), "restored_keys": restored, "conflicts": conflicts}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汉化 Codex Skill 菜单")
    parser.add_argument("--home", type=Path, help="覆盖 CODEX_HOME，仅用于测试")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan")
    scan.add_argument("--json", action="store_true")

    status = subparsers.add_parser("status")
    status.add_argument("--json", action="store_true")

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--dry-run", action="store_true")
    apply_parser.add_argument("--force", action="store_true")

    translate = subparsers.add_parser("translate")
    translate.add_argument("--batch-size", type=int, default=24)
    translate.add_argument("--limit", type=int)
    translate.add_argument("--all", action="store_true")
    translate.add_argument("--apply", action="store_true")
    translate.add_argument("--dry-run-apply", action="store_true")

    lookup = subparsers.add_parser("lookup")
    lookup.add_argument("query")
    lookup.add_argument("--json", action="store_true")

    guide_show = subparsers.add_parser("guide-show")
    guide_show.add_argument("--key", required=True)

    guide_save = subparsers.add_parser("guide-save")
    guide_save.add_argument("--key", required=True)
    guide_save.add_argument("--content-file", type=Path, required=True)

    restore = subparsers.add_parser("restore")
    restore.add_argument("--skill")
    restore.add_argument("--all", action="store_true")
    restore.add_argument("--force", action="store_true")

    args = parser.parse_args(argv)
    home = (args.home or codex_home()).resolve()
    if home != codex_home() and not args.home:
        raise RuntimeError("内部路径解析错误")

    if args.command == "scan":
        records = [record.public() for record in discover_skills(home)]
        if args.json:
            print(json.dumps(records, ensure_ascii=False, indent=2))
        else:
            for record in records:
                print(
                    f"{record.key}\t{record.current_display_name or record.skill_name}\t"
                    f"{record.current_short_description or '未汉化'}"
                )
            print(f"共 {len(records)} 个 Skill", file=sys.stderr)
        return 0

    if args.command == "status":
        report = status_report(home)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(
                f"共 {report['total']} 个，已汉化 {report['translated']} 个，"
                f"待汉化 {report['pending']} 个，说明缓存 {report['guides']} 个，"
                f"冲突 {len(report['conflicts'])} 个"
            )
        return 0

    if args.command == "apply":
        report = apply_translations(home=home, dry_run=args.dry_run, force=args.force)
        reports_dir = home / "skill-han" / "reports"
        reports_dir.mkdir(parents=True, exist_ok=True)
        save_json(reports_dir / "apply-latest.json", report)
        print(
            f"共 {report['total']} 个，应用 {report['applied']} 个，"
            f"无需改动 {report['unchanged']} 个，待汉化 {report['pending']} 个，"
            f"冲突 {len(report['conflicts'])} 个"
        )
        if report["conflicts"]:
            for conflict in report["conflicts"][:20]:
                print(f"冲突：{conflict['key']}：{conflict['reason']}")
        return 2 if report["conflicts"] else 0

    if args.command == "translate":
        limit = None if args.all else (args.limit if args.limit is not None else 24)
        report = translate_pending(
            home=home,
            batch_size=max(1, args.batch_size),
            limit=limit,
            apply=args.apply,
            dry_run_apply=args.dry_run_apply,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if not report["rejected"] else 2

    if args.command == "lookup":
        matches = lookup_records(home, args.query)
        payload = []
        for record in matches:
            item = record.public()
            item["guide"] = guide_status(home, record)
            payload.append(item)
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        elif not payload:
            print("没有找到匹配的 Skill")
        else:
            for item in payload:
                print(f"{item['key']}\t{item['skill_name']}\t{item['skill_file']}")
        return 0 if payload else 1

    if args.command == "guide-show":
        path = guide_path_for(home, args.key)
        if not path.exists():
            print(f"没有缓存说明：{args.key}", file=sys.stderr)
            return 1
        print(read_text(path))
        return 0

    if args.command == "guide-save":
        matches = [record for record in discover_skills(home) if record.key == args.key]
        if len(matches) != 1:
            raise RuntimeError("guide-save 需要唯一的完整 key")
        record = matches[0]
        content = read_text(args.content_file).strip()
        if not content:
            raise RuntimeError("说明内容为空")
        path = guide_path_for(home, record.key)
        atomic_write_text(path, content + "\n")
        save_json(
            guide_metadata_path(home, record.key),
            {
                "key": record.key,
                "skill_file": record.skill_file,
                "source_hash": record.source_hash,
                "saved_at": utc_now(),
            },
        )
        print(str(path))
        return 0

    if args.command == "restore":
        if bool(args.skill) == bool(args.all):
            parser.error("restore 必须二选一：--skill 或 --all")
        report = restore_targets(
            home=home,
            skill_key=args.skill,
            restore_all=args.all,
            force=args.force,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if report["conflicts"] else 0

    return 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        raise SystemExit(1)
