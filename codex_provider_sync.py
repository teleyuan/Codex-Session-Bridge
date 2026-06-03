#!/usr/bin/env python3
"""Synchronize Codex session providers across local history.

This tool retags local Codex threads so the current ``model_provider`` can see
sessions that were originally created under another provider. It updates the
SQLite state index and the rollout JSONL session metadata, with backups and a
with zip backups before applying changes.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

try:  # Python 3.11+
    import tomllib  # type: ignore[import-not-found]
except ModuleNotFoundError:  # pragma: no cover - only used on older Python.
    tomllib = None  # type: ignore[assignment]


TOOL_VERSION = "0.2.1"
STATE_DB_NAME = "state_5.sqlite"
BACKUP_DIR_NAME = "provider-sync-backups"
REQUIRED_THREAD_COLUMNS = {"id", "model_provider", "rollout_path"}


class SyncError(RuntimeError):
    """Raised for user-actionable sync failures."""


@dataclass(frozen=True)
class CodexPaths:
    codex_home: Path
    config_path: Path
    sqlite_home: Path
    state_db: Path
    backup_dir: Path


@dataclass(frozen=True)
class ThreadRow:
    thread_id: str
    model_provider: str
    rollout_path: str
    model: str | None
    archived: int | None


def utc_timestamp() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def utc_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def expand_path(value: str, base: Path) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(value))
    path = Path(expanded)
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if tomllib is not None:
        with path.open("rb") as config_file:
            return tomllib.load(config_file)
    return load_minimal_toml(path)


def load_minimal_toml(path: Path) -> dict[str, Any]:
    """Fallback parser for simple string keys used by this tool.

    It intentionally supports only the small TOML subset we need when Python is
    older than 3.11: top-level string keys and ``[profiles.<name>]`` sections.
    """

    result: dict[str, Any] = {}
    current: dict[str, Any] = result
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1].strip()
            parts = [part.strip() for part in section.split(".") if part.strip()]
            current = result
            for part in parts:
                current = current.setdefault(part, {})
            continue
        if "=" not in line:
            continue
        key, value = [part.strip() for part in line.split("=", 1)]
        if value.startswith(('"', "'")) and value.endswith(('"', "'")):
            current[key] = value[1:-1]
    return result


def resolve_paths(codex_home_override: str | None = None) -> CodexPaths:
    codex_home_raw = codex_home_override or os.environ.get("CODEX_HOME")
    codex_home = expand_path(codex_home_raw, Path.home()) if codex_home_raw else Path.home() / ".codex"
    codex_home = codex_home.resolve()
    config_path = codex_home / "config.toml"
    config = load_toml(config_path)

    sqlite_home_raw = os.environ.get("CODEX_SQLITE_HOME") or config.get("sqlite_home")
    sqlite_home = expand_path(str(sqlite_home_raw), codex_home) if sqlite_home_raw else codex_home
    state_db = sqlite_home / STATE_DB_NAME
    backup_dir = codex_home / BACKUP_DIR_NAME
    return CodexPaths(
        codex_home=codex_home,
        config_path=config_path,
        sqlite_home=sqlite_home,
        state_db=state_db,
        backup_dir=backup_dir,
    )


def current_provider(
    config_path: Path,
    explicit_provider: str | None = None,
    profile: str | None = None,
) -> str:
    if explicit_provider:
        return explicit_provider.strip()
    config = load_toml(config_path)
    if profile:
        profiles = config.get("profiles", {})
        profile_config = profiles.get(profile, {}) if isinstance(profiles, dict) else {}
        provider = profile_config.get("model_provider") if isinstance(profile_config, dict) else None
        if provider:
            return str(provider)
    provider = config.get("model_provider")
    if provider:
        return str(provider)
    raise SyncError(
        "找不到当前 model_provider。请在 config.toml 设置 model_provider，"
        "或运行时传入 --provider <name>。"
    )


def connect_state_db(path: Path, read_only: bool = False) -> sqlite3.Connection:
    if not path.exists():
        raise SyncError(f"找不到 Codex 状态库：{path}")
    if read_only:
        uri = f"file:{path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=10)
    else:
        connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 30000")
    validate_schema(connection)
    return connection


def validate_schema(connection: sqlite3.Connection) -> None:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "threads" not in tables:
        raise SyncError("state_5.sqlite 中没有 threads 表，可能不是 Codex 状态库。")
    columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
    missing = REQUIRED_THREAD_COLUMNS - columns
    if missing:
        missing_text = ", ".join(sorted(missing))
        raise SyncError(f"threads 表缺少必要字段：{missing_text}")


def provider_counts(connection: sqlite3.Connection) -> list[tuple[str, int]]:
    rows = connection.execute(
        """
        SELECT model_provider, COUNT(*) AS count
        FROM threads
        GROUP BY model_provider
        ORDER BY count DESC, model_provider ASC
        """
    ).fetchall()
    return [(str(row["model_provider"]), int(row["count"])) for row in rows]


def provider_names(paths: CodexPaths, current: str | None = None) -> list[str]:
    config = load_toml(paths.config_path)
    names: list[str] = []

    def add_name(value: Any) -> None:
        if value is None:
            return
        name = str(value).strip()
        if name and name not in names:
            names.append(name)

    add_name(config.get("model_provider"))

    model_providers = config.get("model_providers")
    if isinstance(model_providers, dict):
        for name in model_providers.keys():
            add_name(name)

    profiles = config.get("profiles")
    if isinstance(profiles, dict):
        for profile in profiles.values():
            if isinstance(profile, dict):
                add_name(profile.get("model_provider"))

    add_name(current)
    return names


def load_threads(connection: sqlite3.Connection, include_archived: bool = True) -> list[ThreadRow]:
    columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
    archived_expr = "archived" if "archived" in columns else "NULL AS archived"
    model_expr = "model" if "model" in columns else "NULL AS model"
    where = "" if include_archived else "WHERE archived = 0"
    rows = connection.execute(
        f"""
        SELECT id, model_provider, rollout_path, {model_expr}, {archived_expr}
        FROM threads
        {where}
        ORDER BY updated_at DESC
        """
    ).fetchall()
    return [
        ThreadRow(
            thread_id=str(row["id"]),
            model_provider=str(row["model_provider"]),
            rollout_path=str(row["rollout_path"]),
            model=str(row["model"]) if row["model"] is not None else None,
            archived=int(row["archived"]) if row["archived"] is not None else None,
        )
        for row in rows
    ]


def copy_sqlite_database(state_db: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(state_db, timeout=30)
    try:
        backup_connection = sqlite3.connect(destination)
        try:
            source.backup(backup_connection)
        finally:
            backup_connection.close()
    finally:
        source.close()


def safe_filename_fragment(value: str) -> str:
    fragment = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return fragment.strip("._") or "provider"


def archive_name_for_path(path: Path) -> str:
    normalized = str(path)
    if normalized.startswith("\\\\?\\"):
        normalized = normalized[4:]
    resolved = Path(normalized).resolve()
    drive = resolved.drive.rstrip(":\\/")
    parts = list(resolved.parts)
    if resolved.drive and parts:
        parts = parts[1:]
    root = drive or "relative"
    return str(PurePosixPath(root, *parts))


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for index in range(2, 1000):
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise SyncError(f"无法生成不重复的备份文件名：{path}")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def read_session_meta_provider(path: Path) -> tuple[str | None, str | None, str | None]:
    if not path.exists() or not path.is_file():
        return None, None, "missing"
    try:
        with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
            first_line = handle.readline()
    except OSError as exc:
        return None, None, f"read_error: {exc}"
    if not first_line:
        return None, None, "empty"
    try:
        event = json.loads(first_line)
    except json.JSONDecodeError as exc:
        return None, sha256_text(first_line), f"json_error: {exc}"
    payload = event.get("payload") if isinstance(event, dict) else None
    if event.get("type") != "session_meta" or not isinstance(payload, dict):
        return None, sha256_text(first_line), "not_session_meta"
    provider = payload.get("model_provider")
    return str(provider) if provider is not None else None, sha256_text(first_line), None


def update_rollout_provider(path: Path, target_provider: str) -> bool:
    if not path.exists() or not path.is_file():
        return False
    parent = path.parent
    temp_fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=parent, text=True
    )
    changed = False
    try:
        with os.fdopen(temp_fd, "w", encoding="utf-8", newline="") as output:
            with path.open("r", encoding="utf-8", errors="replace", newline="") as source:
                first_line = source.readline()
                if not first_line:
                    output.write(first_line)
                else:
                    try:
                        event = json.loads(first_line)
                        payload = event.get("payload") if isinstance(event, dict) else None
                        if event.get("type") == "session_meta" and isinstance(payload, dict):
                            if payload.get("model_provider") != target_provider:
                                payload["model_provider"] = target_provider
                                changed = True
                            output.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                            output.write("\n")
                        else:
                            output.write(first_line)
                    except json.JSONDecodeError:
                        output.write(first_line)
                shutil.copyfileobj(source, output)
        if changed:
            os.replace(temp_name, path)
        else:
            os.remove(temp_name)
        return changed
    except Exception:
        try:
            os.remove(temp_name)
        except OSError:
            pass
        raise


def codex_processes() -> list[str]:
    if os.name != "nt":
        return []

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    create_snapshot.restype = ctypes.c_void_p
    process_first = kernel32.Process32FirstW
    process_first.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry32)]
    process_first.restype = ctypes.c_int
    process_next = kernel32.Process32NextW
    process_next.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry32)]
    process_next.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    snapshot = create_snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        return []

    processes: list[str] = []
    try:
        entry = ProcessEntry32()
        entry.dwSize = ctypes.sizeof(ProcessEntry32)
        if not process_first(snapshot, ctypes.byref(entry)):
            return []
        while True:
            exe_name = entry.szExeFile
            if exe_name.lower() == "codex.exe":
                processes.append(f"{exe_name} pid={entry.th32ProcessID}")
            if not process_next(snapshot, ctypes.byref(entry)):
                break
    finally:
        close_handle(snapshot)
    return processes


def summarize_status(paths: CodexPaths, provider: str | None, include_archived: bool = True) -> str:
    lines = [
        "Codex 会话同步状态",
        "",
        "路径信息：",
        f"  Codex home : {paths.codex_home}",
        f"  Config     : {paths.config_path}",
        f"  State DB   : {paths.state_db}",
    ]
    if provider:
        lines.append(f"  当前 provider : {provider}")
    with connect_state_db(paths.state_db, read_only=True) as connection:
        counts = provider_counts(connection)
        threads = load_threads(connection, include_archived=include_archived)
    lines.append("")
    lines.append("供应商会话数量：")
    if counts:
        for name, count in counts:
            marker = "  <= 当前选择" if provider and name == provider else ""
            lines.append(f"  {name:<18} {count:>5}{marker}")
    else:
        lines.append("  (没有会话记录)")
    archived_count = sum(1 for thread in threads if thread.archived == 1)
    lines.append("")
    lines.append("扫描摘要：")
    lines.append(f"  会话记录总数 : {len(threads)}")
    if archived_count:
        lines.append(f"  已归档会话   : {archived_count}")
    running = codex_processes()
    if running:
        lines.append("")
        lines.append("Warning: 检测到 codex.exe 正在运行。建议关闭 Codex 后再同步。")
    return "\n".join(lines)


def build_sync_plan(
    paths: CodexPaths,
    target_provider: str,
    include_archived: bool = True,
    update_rollouts: bool = True,
    read_only: bool = True,
) -> tuple[list[ThreadRow], list[dict[str, Any]], dict[str, Any]]:
    if not target_provider:
        raise SyncError("目标 model_provider 不能为空。")
    with connect_state_db(paths.state_db, read_only=read_only) as connection:
        threads = load_threads(connection, include_archived=include_archived)
        rows_to_update = [thread for thread in threads if thread.model_provider != target_provider]

    rollout_entries: list[dict[str, Any]] = []
    if update_rollouts:
        for thread in rows_to_update:
            if not thread.rollout_path:
                continue
            path = Path(thread.rollout_path)
            provider, first_line_sha256, warning = read_session_meta_provider(path)
            entry = {
                "thread_id": thread.thread_id,
                "path": str(path),
                "old_model_provider": provider,
                "new_model_provider": target_provider,
                "first_line_sha256_before": first_line_sha256,
            }
            if warning:
                entry["warning"] = warning
            if provider != target_provider or warning:
                rollout_entries.append(entry)

    manifest = {
        "tool": "codex-provider-sync",
        "tool_version": TOOL_VERSION,
        "created_at": utc_iso(),
        "target_provider": target_provider,
        "codex_home": str(paths.codex_home),
        "sqlite_home": str(paths.sqlite_home),
        "state_db": str(paths.state_db),
        "include_archived": include_archived,
        "update_rollouts": update_rollouts,
        "updated_threads": [
            {
                "id": thread.thread_id,
                "old_model_provider": thread.model_provider,
                "new_model_provider": target_provider,
                "rollout_path": thread.rollout_path,
                "model": thread.model,
                "archived": thread.archived,
            }
            for thread in rows_to_update
        ],
        "rollout_entries": rollout_entries,
    }
    return rows_to_update, rollout_entries, manifest


def create_sync_backup_zip(
    paths: CodexPaths,
    target_provider: str,
    include_archived: bool = True,
    update_rollouts: bool = True,
    rows_to_update: list[ThreadRow] | None = None,
    rollout_entries: list[dict[str, Any]] | None = None,
    manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if rows_to_update is None or rollout_entries is None or manifest is None:
        rows_to_update, rollout_entries, manifest = build_sync_plan(
            paths,
            target_provider,
            include_archived=include_archived,
            update_rollouts=update_rollouts,
            read_only=True,
        )

    paths.backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = utc_timestamp()
    provider_fragment = safe_filename_fragment(target_provider)
    zip_path = unique_path(
        paths.backup_dir / f"provider-sync-backup-{timestamp}-to-{provider_fragment}.zip"
    )
    temp_db = paths.backup_dir / f".{STATE_DB_NAME}.{timestamp}.tmp"
    copy_sqlite_database(paths.state_db, temp_db)

    included_files: list[dict[str, str]] = []
    missing_files: list[str] = []
    seen_archive_names: set[str] = set()

    def add_file(zip_file: zipfile.ZipFile, source: Path, archive_name: str, kind: str) -> None:
        if archive_name in seen_archive_names:
            return
        seen_archive_names.add(archive_name)
        zip_file.write(source, archive_name)
        included_files.append({"kind": kind, "source": str(source), "archive_name": archive_name})

    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zip_file:
            add_file(zip_file, temp_db, archive_name_for_path(paths.state_db), "sqlite_snapshot")
            if update_rollouts:
                for thread in rows_to_update:
                    if not thread.rollout_path:
                        continue
                    rollout_path = Path(thread.rollout_path)
                    if rollout_path.exists() and rollout_path.is_file():
                        add_file(
                            zip_file,
                            rollout_path,
                            archive_name_for_path(rollout_path),
                            "rollout_jsonl",
                        )
                    else:
                        missing_files.append(str(rollout_path))
            backup_manifest = {
                **manifest,
                "backup_created_at": utc_iso(),
                "zip_path": str(zip_path),
                "included_files": included_files,
                "missing_files": missing_files,
                "note": "SQLite 文件为通过 SQLite backup API 生成的一致性快照，压缩包内路径保留原目录层级。",
            }
            zip_file.writestr(
                "backup-manifest.json",
                json.dumps(backup_manifest, ensure_ascii=False, indent=2),
            )
    finally:
        try:
            temp_db.unlink()
        except OSError:
            pass

    return {
        "zip_path": str(zip_path),
        "included_files": included_files,
        "missing_files": missing_files,
        "threads_to_update": len(rows_to_update),
        "rollout_entries": rollout_entries,
        "target_provider": target_provider,
    }


def sync_to_provider(
    paths: CodexPaths,
    target_provider: str,
    apply: bool,
    include_archived: bool = True,
    update_rollouts: bool = True,
    allow_running_codex: bool = False,
) -> dict[str, Any]:
    if not target_provider:
        raise SyncError("目标 model_provider 不能为空。")
    running = codex_processes()
    if apply and running and not allow_running_codex:
        raise SyncError(
            "检测到 codex.exe 正在运行。为避免锁库或状态竞争，请关闭 Codex 后重试；"
            "如果确认要继续，请加 --allow-running-codex。"
        )

    rows_to_update, rollout_entries, manifest = build_sync_plan(
        paths,
        target_provider,
        include_archived=include_archived,
        update_rollouts=update_rollouts,
        read_only=not apply,
    )
    manifest["dry_run"] = not apply

    if not apply:
        return manifest

    backup_result = create_sync_backup_zip(
        paths,
        target_provider,
        include_archived=include_archived,
        update_rollouts=update_rollouts,
        rows_to_update=rows_to_update,
        rollout_entries=rollout_entries,
        manifest=manifest,
    )
    manifest["backup_zip"] = backup_result["zip_path"]
    manifest["backup_included_files"] = backup_result["included_files"]
    manifest["backup_missing_files"] = backup_result["missing_files"]

    with connect_state_db(paths.state_db, read_only=False) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if rows_to_update:
                ids = [thread.thread_id for thread in rows_to_update]
                connection.executemany(
                    "UPDATE threads SET model_provider = ? WHERE id = ?",
                    [(target_provider, thread_id) for thread_id in ids],
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    rollout_changed = 0
    if update_rollouts:
        for entry in rollout_entries:
            if entry.get("warning") == "missing":
                continue
            path = Path(entry["path"])
            if update_rollout_provider(path, target_provider):
                rollout_changed += 1
    manifest["rollout_files_changed"] = rollout_changed
    return manifest


def format_sync_result(result: dict[str, Any]) -> str:
    dry_run = result.get("dry_run", True)
    lines = ["同步预演结果：" if dry_run else "同步完成："]
    lines.append(f"  目标 provider        : {result.get('target_provider')}")
    lines.append(f"  待更新会话记录       : {len(result.get('updated_threads', []))}")
    lines.append(f"  待检查会话文件       : {len(result.get('rollout_entries', []))}")
    if result.get("backup_zip"):
        lines.append(f"  备份压缩包           : {result['backup_zip']}")
    if "rollout_files_changed" in result:
        lines.append(f"  已修改会话文件       : {result['rollout_files_changed']}")
    warnings = [entry for entry in result.get("rollout_entries", []) if entry.get("warning")]
    if warnings:
        lines.append(f"Warning: 有 {len(warnings)} 个会话文件存在读取或格式异常，详见备份清单。")
    return "\n".join(lines)


def format_backup_result(result: dict[str, Any]) -> str:
    lines = ["备份完成："]
    lines.append(f"  目标 provider        : {result.get('target_provider')}")
    lines.append(f"  涉及会话记录         : {result.get('threads_to_update')}")
    lines.append(f"  打包文件数量         : {len(result.get('included_files', []))}")
    lines.append(f"  备份压缩包           : {result.get('zip_path')}")
    missing_files = result.get("missing_files", [])
    if missing_files:
        lines.append(f"Warning: 有 {len(missing_files)} 个会话文件不存在，未能加入压缩包。")
    return "\n".join(lines)


def run_gui(paths: CodexPaths, provider: str | None) -> int:
    try:
        import tkinter as tk
        from tkinter import messagebox, scrolledtext, ttk
    except Exception as exc:  # pragma: no cover - environment dependent.
        raise SyncError(f"无法启动图形界面：{exc}") from exc

    root = tk.Tk()
    root.title("Codex 会话供应商同步工具")
    root.geometry("980x640")
    root.minsize(860, 520)

    provider_var = tk.StringVar(value=provider or "")
    providers = provider_names(paths, provider)

    header = tk.Frame(root)
    header.pack(fill=tk.X, padx=12, pady=(12, 6))
    title = tk.Label(header, text="Codex 会话供应商同步工具", font=("Microsoft YaHei UI", 15, "bold"))
    title.pack(anchor="w")
    subtitle = tk.Label(
        header,
        text="选择目标 provider 后，可先备份待改动文件，再同步会话索引与 JSONL 元数据。",
        fg="#555555",
        font=("Microsoft YaHei UI", 9),
    )
    subtitle.pack(anchor="w", pady=(3, 0))

    controls = tk.Frame(root)
    controls.pack(fill=tk.X, padx=12, pady=(0, 8))
    tk.Label(controls, text="目标 provider:").pack(side=tk.LEFT)
    provider_combo = ttk.Combobox(
        controls,
        textvariable=provider_var,
        values=providers,
        width=28,
        state="readonly" if providers else "normal",
    )
    provider_combo.pack(side=tk.LEFT, padx=(6, 12))
    if provider and provider in providers:
        provider_combo.set(provider)
    elif providers:
        provider_combo.current(0)

    text = scrolledtext.ScrolledText(
        root,
        wrap=tk.WORD,
        font=("Consolas", 10),
        background="#fbfbfb",
        foreground="#222222",
        insertbackground="#222222",
        relief=tk.SOLID,
        borderwidth=1,
    )
    text.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 10))
    text.tag_configure("warning", foreground="#b00020", font=("Consolas", 10, "bold"))
    text.tag_configure("heading", foreground="#0b5394", font=("Consolas", 10, "bold"))
    text.tag_configure("success", foreground="#137333", font=("Consolas", 10, "bold"))
    text.tag_configure("path", foreground="#444444")

    def set_output(content: str) -> None:
        text.configure(state=tk.NORMAL)
        text.delete("1.0", tk.END)
        for line in content.splitlines(True):
            stripped = line.strip()
            tag: str | None = None
            if stripped.startswith("Warning:") or stripped.startswith("错误："):
                tag = "warning"
            elif stripped.endswith("：") or stripped in {"Codex 会话同步状态"}:
                tag = "heading"
            elif stripped.startswith(("同步完成", "备份完成")):
                tag = "success"
            elif ":\\" in line or "C:/" in line or ".zip" in line:
                tag = "path"
            text.insert(tk.END, line, tag)
        text.configure(state=tk.DISABLED)
        text.see(tk.END)

    def selected_provider() -> str:
        return provider_var.get().strip()

    def refresh_provider_values() -> None:
        try:
            config_provider = current_provider(paths.config_path)
        except SyncError:
            config_provider = None
        current = selected_provider()
        try:
            names = provider_names(paths, config_provider or current or None)
        except Exception:
            names = []
        provider_combo.configure(values=names, state="readonly" if names else "normal")
        if current in names:
            provider_combo.set(current)
        elif config_provider and config_provider in names:
            provider_combo.set(config_provider)
        elif names:
            provider_combo.current(0)

    def refresh() -> None:
        refresh_provider_values()
        detected = selected_provider()
        if not detected:
            try:
                detected = current_provider(paths.config_path)
                provider_var.set(detected)
            except SyncError:
                detected = None
        set_output(summarize_status(paths, detected))

    def backup_clicked() -> None:
        target = selected_provider()
        if not target:
            messagebox.showerror("缺少 provider", "请选择目标 model_provider。")
            return
        try:
            rows_to_update, rollout_entries, manifest = build_sync_plan(
                paths,
                target,
                include_archived=True,
                update_rollouts=True,
                read_only=True,
            )
            if not rows_to_update:
                set_output(f"备份预检查：\n  目标 provider        : {target}\n  没有需要改动的会话记录。")
                messagebox.showinfo("无需备份", "当前没有需要改动的会话记录。")
                return
            if not messagebox.askyesno(
                "确认备份",
                f"将打包 {len(rows_to_update)} 条待改动会话对应的数据库快照和会话文件。\n是否继续？",
            ):
                return
            result = create_sync_backup_zip(
                paths,
                target,
                include_archived=True,
                update_rollouts=True,
                rows_to_update=rows_to_update,
                rollout_entries=rollout_entries,
                manifest=manifest,
            )
            set_output(format_backup_result(result) + "\n\n" + summarize_status(paths, target))
            messagebox.showinfo("备份完成", f"已生成压缩包：\n{result['zip_path']}")
        except Exception as exc:
            messagebox.showerror("备份失败", str(exc))
            set_output(f"错误：备份失败：{exc}")

    def sync_clicked() -> None:
        target = selected_provider()
        if not target:
            messagebox.showerror("缺少 provider", "请选择目标 model_provider。")
            return
        running = codex_processes()
        allow_running = False
        if running:
            allow_running = messagebox.askyesno(
                "Codex 正在运行",
                "检测到 codex.exe 正在运行。建议先关闭 Codex。\n仍要继续同步吗？",
            )
            if not allow_running:
                return
        try:
            preview = sync_to_provider(
                paths,
                target,
                apply=False,
                include_archived=True,
                update_rollouts=True,
                allow_running_codex=True,
            )
            count = len(preview.get("updated_threads", []))
            if count == 0:
                set_output(format_sync_result(preview) + "\n\n" + summarize_status(paths, target))
                messagebox.showinfo("无需同步", "当前没有需要同步的会话记录。")
                return
            if not messagebox.askyesno(
                "确认同步",
                f"将 {count} 条会话同步到 provider: {target}\n同步前会自动生成 zip 备份。\n是否继续？",
            ):
                return
            result = sync_to_provider(
                paths,
                target,
                apply=True,
                include_archived=True,
                update_rollouts=True,
                allow_running_codex=allow_running,
            )
            refresh_provider_values()
            set_output(format_sync_result(result) + "\n\n" + summarize_status(paths, target))
            messagebox.showinfo("同步完成", f"同步完成，已生成备份压缩包：\n{result.get('backup_zip')}")
        except Exception as exc:
            messagebox.showerror("同步失败", str(exc))
            set_output(f"错误：同步失败：{exc}")

    tk.Button(controls, text="刷新状态", command=refresh).pack(side=tk.LEFT, padx=4)
    tk.Button(controls, text="备份改动文件", command=backup_clicked).pack(side=tk.LEFT, padx=4)
    tk.Button(controls, text="同步到此 provider", command=sync_clicked).pack(side=tk.LEFT, padx=4)
    tk.Button(controls, text="退出", command=root.destroy).pack(side=tk.RIGHT, padx=4)

    refresh()
    root.mainloop()
    return 0

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="让 Codex 不同 model_provider 共享本地 resume 会话。"
    )
    parser.add_argument("--codex-home", help="Codex home，默认读取 CODEX_HOME 或 ~/.codex。")
    parser.add_argument("--provider", help="目标 model_provider；默认读取 config.toml。")
    parser.add_argument("--profile", help="从 config.toml 的指定 profile 读取 model_provider。")
    parser.add_argument("--gui", action="store_true", help="启动图形界面。")

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="查看当前 provider 和 threads 分布。")

    sync_parser = subparsers.add_parser("sync", help="同步所有会话到当前/指定 provider。")
    sync_parser.add_argument("--apply", action="store_true", help="真正写入；不加时只预演。")
    sync_parser.add_argument("--skip-rollouts", action="store_true", help="只改 SQLite，不改 JSONL 元数据。")
    sync_parser.add_argument("--skip-archived", action="store_true", help="跳过 archived=1 的 thread 行。")
    sync_parser.add_argument(
        "--allow-running-codex",
        action="store_true",
        help="允许在 codex.exe 运行时写库；不建议。",
    )

    backup_parser = subparsers.add_parser("backup", help="把待改动数据库快照和会话文件打包成 zip。")
    backup_parser.add_argument("--skip-rollouts", action="store_true", help="只备份 SQLite 快照，不备份 JSONL 会话文件。")
    backup_parser.add_argument("--skip-archived", action="store_true", help="跳过 archived=1 的 thread 行。")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        paths = resolve_paths(args.codex_home)
        provider: str | None = None
        try:
            provider = current_provider(paths.config_path, args.provider, args.profile)
        except SyncError:
            raise
        if args.gui:
            return run_gui(paths, provider)

        command = args.command or "status"
        if command == "status":
            print(summarize_status(paths, provider))
            return 0
        if command == "sync":
            if provider is None:
                raise SyncError("同步需要目标 provider。请传入 --provider 或配置 model_provider。")
            result = sync_to_provider(
                paths,
                provider,
                apply=args.apply,
                include_archived=not args.skip_archived,
                update_rollouts=not args.skip_rollouts,
                allow_running_codex=args.allow_running_codex,
            )
            print(format_sync_result(result))
            if not args.apply:
                print("\n未写入任何内容；确认无误后加 sync --apply。")
            return 0
        if command == "backup":
            if provider is None:
                raise SyncError("备份需要目标 provider。请传入 --provider 或配置 model_provider。")
            result = create_sync_backup_zip(
                paths,
                provider,
                include_archived=not args.skip_archived,
                update_rollouts=not args.skip_rollouts,
            )
            print(format_backup_result(result))
            return 0
        parser.error(f"未知命令：{command}")
        return 2
    except SyncError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())




