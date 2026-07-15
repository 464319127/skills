#!/usr/bin/env python3
"""Export a conversation as a verified archive and OpenAI-compatible JSON."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXPORTER_NAME = "export-conversation"
EXPORTER_VERSION = "1.0.0"


class ExportError(RuntimeError):
    pass


@dataclass(frozen=True)
class JsonlRecord:
    value: dict[str, Any]
    line_number: int
    end_offset: int


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_info(path: Path, root: Path) -> dict[str, Any]:
    data = path.read_bytes()
    return {
        "path": path.relative_to(root).as_posix(),
        "bytes": len(data),
        "sha256": sha256_bytes(data),
    }


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def snapshot_file(path: Path) -> bytes:
    try:
        with path.open("rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            data = handle.read(size)
    except OSError as exc:
        raise ExportError(f"Cannot read input file {path}: {exc}") from exc
    if len(data) != size:
        raise ExportError(f"Could not take a stable snapshot of {path}")
    return data


def parse_jsonl(data: bytes) -> tuple[list[JsonlRecord], list[str]]:
    records: list[JsonlRecord] = []
    warnings: list[str] = []
    offset = 0
    lines = data.splitlines(keepends=True)
    for index, raw_line in enumerate(lines, start=1):
        offset += len(raw_line)
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            is_last = index == len(lines)
            if is_last and not data.endswith((b"\n", b"\r")):
                warnings.append(
                    f"Ignored an incomplete trailing JSONL record at line {index}; "
                    "the raw snapshot still contains its bytes."
                )
                break
            raise ExportError(f"Invalid JSONL at line {index}: {exc}") from exc
        if not isinstance(value, dict):
            raise ExportError(f"JSONL line {index} is not an object")
        records.append(JsonlRecord(value, index, offset))
    if not records:
        raise ExportError("The input contains no JSON records")
    return records, warnings


def load_source(data: bytes) -> tuple[str, Any, list[JsonlRecord], list[str]]:
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        records, warnings = parse_jsonl(data)
        if any(record.value.get("type") in {"session_meta", "event_msg"} for record in records):
            return "codex-rollout", None, records, warnings
        if len(records) == 1 and isinstance(records[0].value.get("messages"), list):
            return "openai-chat", records[0].value, records, warnings
        raise ExportError(
            "Unsupported JSONL. Expected a Codex rollout or one object containing messages."
        )

    if isinstance(parsed, dict) and isinstance(parsed.get("messages"), list):
        return "openai-chat", parsed, [], []
    if isinstance(parsed, list) and all(
        isinstance(item, dict) and isinstance(item.get("role"), str) for item in parsed
    ):
        return "openai-chat", {"messages": parsed}, [], []
    raise ExportError(
        "Unsupported JSON. Expected {\"messages\": [...]}, a message array, or a Codex rollout."
    )


def find_codex_rollout(codex_home: Path, thread_id: str) -> Path:
    sessions = codex_home.expanduser().resolve() / "sessions"
    if not sessions.is_dir():
        raise ExportError(f"Codex sessions directory not found: {sessions}")
    matches = sorted(sessions.rglob(f"*{thread_id}*.jsonl"))
    if not matches:
        raise ExportError(f"No Codex rollout found for thread {thread_id} under {sessions}")
    if len(matches) > 1:
        rendered = "\n".join(f"  - {path}" for path in matches)
        raise ExportError(f"Multiple rollouts matched thread {thread_id}:\n{rendered}")
    return matches[0]


def session_metadata(records: list[JsonlRecord]) -> dict[str, Any]:
    for record in records:
        if record.value.get("type") != "session_meta":
            continue
        payload = record.value.get("payload") or {}
        return {
            "conversation_id": payload.get("session_id") or payload.get("id"),
            "started_at": record.value.get("timestamp") or payload.get("timestamp"),
            "source": payload.get("source"),
            "cli_version": payload.get("cli_version"),
            "cwd": payload.get("cwd"),
        }
    return {}


def completed_cutoff(
    records: list[JsonlRecord], include_incomplete: bool
) -> tuple[list[JsonlRecord], int, str | None, bool]:
    if include_incomplete:
        last = records[-1]
        return records, last.end_offset, last.value.get("timestamp"), False

    completed = [
        record
        for record in records
        if record.value.get("type") == "event_msg"
        and (record.value.get("payload") or {}).get("type") == "task_complete"
    ]
    if not completed:
        raise ExportError(
            "The Codex rollout has no completed turn. Retry after the turn finishes or use "
            "--include-incomplete."
        )
    last_complete = completed[-1]
    selected = [record for record in records if record.end_offset <= last_complete.end_offset]
    return selected, last_complete.end_offset, last_complete.value.get("timestamp"), True


def safe_asset_name(source: Path, digest: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9._-]+", "-", source.name).strip("-.") or "asset"
    return f"{digest[:16]}-{clean}"


def copy_local_asset(
    source_value: Any,
    assets_dir: Path,
    asset_cache: dict[str, dict[str, Any]],
    allow_missing: bool,
    warnings: list[str],
) -> tuple[dict[str, Any], str | None]:
    source_text = str(source_value)
    source = Path(source_text).expanduser()
    if not source.is_absolute():
        source = source.resolve()
    try:
        data = source.read_bytes()
    except OSError as exc:
        message = f"Could not copy attachment {source_text}: {exc}"
        if not allow_missing:
            raise ExportError(message) from exc
        warnings.append(message)
        return {"source": source_text, "status": "missing"}, None

    digest = sha256_bytes(data)
    if digest in asset_cache:
        cached = asset_cache[digest]
        return {**cached, "source": source_text, "status": "copied"}, cached["data_url"]

    destination = assets_dir / safe_asset_name(source, digest)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
    data_url = f"data:{mime_type};base64,{base64.b64encode(data).decode('ascii')}"
    cached = {
        "asset": destination.relative_to(assets_dir.parent).as_posix(),
        "bytes": len(data),
        "sha256": digest,
        "media_type": mime_type,
        "data_url": data_url,
    }
    asset_cache[digest] = cached
    return {**cached, "source": source_text, "status": "copied"}, data_url


def remote_image_url(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return None
    image_url = value.get("image_url")
    if isinstance(image_url, str):
        return image_url
    if isinstance(image_url, dict) and isinstance(image_url.get("url"), str):
        return image_url["url"]
    for key in ("url", "data_url"):
        if isinstance(value.get(key), str):
            return value[key]
    return None


def user_message_content(text: str, image_urls: list[str]) -> Any:
    if not image_urls:
        return text
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.extend(
        {"type": "image_url", "image_url": {"url": image_url}}
        for image_url in image_urls
    )
    return parts


def convert_codex(
    records: list[JsonlRecord],
    bundle_root: Path,
    allow_missing_assets: bool,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    events: list[dict[str, Any]] = []
    messages: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    asset_cache: dict[str, dict[str, Any]] = {}
    assets_dir = bundle_root / "assets"

    for record in records:
        value = record.value
        if value.get("type") != "event_msg":
            continue
        payload = value.get("payload") or {}
        event_type = payload.get("type")
        if event_type == "user_message":
            text = payload.get("message") or ""
            remote_urls = [
                url
                for item in payload.get("images") or []
                if (url := remote_image_url(item)) is not None
            ]
            local_assets: list[dict[str, Any]] = []
            local_urls: list[str] = []
            for item in payload.get("local_images") or []:
                asset, data_url = copy_local_asset(
                    item,
                    assets_dir,
                    asset_cache,
                    allow_missing_assets,
                    warnings,
                )
                local_assets.append({key: value for key, value in asset.items() if key != "data_url"})
                if data_url:
                    local_urls.append(data_url)
            attachments = {
                "images": payload.get("images") or [],
                "local_images": local_assets,
                "text_elements": payload.get("text_elements") or [],
            }
            events.append(
                {
                    "source_line": record.line_number,
                    "timestamp": value.get("timestamp"),
                    "role": "user",
                    "content": text,
                    "attachments": attachments,
                }
            )
            messages.append(
                {
                    "role": "user",
                    "content": user_message_content(text, remote_urls + local_urls),
                }
            )
        elif event_type == "agent_message":
            text = payload.get("message") or ""
            event: dict[str, Any] = {
                "source_line": record.line_number,
                "timestamp": value.get("timestamp"),
                "role": "assistant",
                "phase": payload.get("phase"),
                "content": text,
            }
            if payload.get("memory_citation") is not None:
                event["memory_citation"] = payload["memory_citation"]
            events.append(event)
            messages.append({"role": "assistant", "content": text})

    for cached in asset_cache.values():
        assets.append({key: value for key, value in cached.items() if key != "data_url"})
    assets.sort(key=lambda item: item["asset"])
    return events, messages, assets


def convert_openai(source: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    messages = source.get("messages")
    if not isinstance(messages, list):
        raise ExportError("OpenAI input does not contain a messages array")
    normalized: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or not isinstance(message.get("role"), str):
            raise ExportError(f"messages[{index}] must be an object with a role")
        normalized.append(message)
        events.append(
            {
                "source_index": index,
                "role": message["role"],
                "content": message.get("content"),
                "message": message,
            }
        )
    return events, normalized


def collect_file_manifest(root: Path) -> list[dict[str, Any]]:
    return sorted(
        (
            file_info(path, root)
            for path in root.rglob("*")
            if path.is_file() and path.name != "manifest.json"
        ),
        key=lambda item: item["path"],
    )


def default_output(conversation_id: str) -> Path:
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", conversation_id).strip("-.") or "session"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return Path.cwd() / f"conversation-export-{safe_id}-{stamp}"


def export(args: argparse.Namespace) -> dict[str, Any]:
    thread_id = args.thread_id or os.environ.get("CODEX_THREAD_ID")
    if args.input:
        input_path = args.input.expanduser().resolve()
    else:
        if not thread_id:
            raise ExportError(
                "No current Codex thread is available. Pass --thread-id or --input explicitly."
            )
        input_path = find_codex_rollout(args.codex_home, thread_id)

    source_bytes = snapshot_file(input_path)
    source_kind, parsed, records, warnings = load_source(source_bytes)
    metadata: dict[str, Any] = {}
    selected_records = records
    completed_bytes = source_bytes
    cutoff_timestamp: str | None = None
    completed_only = False

    if source_kind == "codex-rollout":
        metadata = session_metadata(records)
        selected_records, cutoff_offset, cutoff_timestamp, completed_only = completed_cutoff(
            records, args.include_incomplete
        )
        completed_bytes = source_bytes[:cutoff_offset]
        conversation_id = metadata.get("conversation_id") or thread_id or input_path.stem
    else:
        conversation_id = (
            parsed.get("conversation_id")
            or parsed.get("id")
            or args.thread_id
            or input_path.stem
        )

    output = (args.output or default_output(str(conversation_id))).expanduser().resolve()
    if output.exists():
        raise ExportError(f"Output path already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.tmp-{os.getpid()}"
    if staging.exists():
        raise ExportError(f"Temporary output path already exists: {staging}")

    try:
        staging.mkdir()
        raw_dir = staging / "raw"
        raw_dir.mkdir()
        source_name = "source" + input_path.suffix.lower()
        (raw_dir / source_name).write_bytes(source_bytes)
        if source_kind == "codex-rollout":
            (raw_dir / "completed.jsonl").write_bytes(completed_bytes)
            events, messages, assets = convert_codex(
                selected_records,
                staging,
                args.allow_missing_assets,
                warnings,
            )
        else:
            events, messages = convert_openai(parsed)
            assets = []

        visible = {
            "schema_version": "1.0",
            "conversation_id": conversation_id,
            "source_kind": source_kind,
            "events": events,
        }
        openai_messages = {"messages": messages}
        write_json(staging / "visible" / "events.json", visible)
        write_json(staging / "openai" / "messages.json", openai_messages)

        manifest = {
            "schema_version": "1.0",
            "exporter": {"name": EXPORTER_NAME, "version": EXPORTER_VERSION},
            "created_at": utc_now(),
            "conversation": {
                "id": conversation_id,
                "source_kind": source_kind,
                "completed_only": completed_only,
                "cutoff_timestamp": cutoff_timestamp,
                "visible_event_count": len(events),
                "openai_message_count": len(messages),
                **{key: value for key, value in metadata.items() if key != "conversation_id"},
            },
            "source": {
                "path": str(input_path),
                "snapshot_file": f"raw/{source_name}",
                "bytes": len(source_bytes),
                "sha256": sha256_bytes(source_bytes),
            },
            "assets": assets,
            "warnings": warnings,
            "files": collect_file_manifest(staging),
        }
        write_json(staging / "manifest.json", manifest)
        os.replace(staging, output)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    return {
        "status": "ok",
        "output": str(output),
        "conversation_id": conversation_id,
        "source_kind": source_kind,
        "completed_only": completed_only,
        "visible_events": len(events),
        "openai_messages": len(messages),
        "warnings": warnings,
    }


def verify(bundle: Path) -> dict[str, Any]:
    root = bundle.expanduser().resolve()
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportError(f"Cannot read manifest {manifest_path}: {exc}") from exc
    failures: list[str] = []
    for expected in manifest.get("files") or []:
        relative = expected.get("path")
        if not isinstance(relative, str):
            failures.append("Manifest contains a file without a valid path")
            continue
        path = (root / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            failures.append(f"Path escapes bundle: {relative}")
            continue
        if not path.is_file():
            failures.append(f"Missing file: {relative}")
            continue
        actual = file_info(path, root)
        if actual["bytes"] != expected.get("bytes"):
            failures.append(f"Size mismatch: {relative}")
        if actual["sha256"] != expected.get("sha256"):
            failures.append(f"SHA-256 mismatch: {relative}")
    return {
        "status": "ok" if not failures else "failed",
        "bundle": str(root),
        "checked_files": len(manifest.get("files") or []),
        "failures": failures,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export a Codex or OpenAI-format conversation as a verified bundle."
    )
    parser.add_argument("--input", type=Path, help="Codex JSONL or OpenAI messages JSON")
    parser.add_argument("--thread-id", help="Codex thread/session ID")
    parser.add_argument(
        "--codex-home",
        type=Path,
        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")),
        help="Codex home directory (default: CODEX_HOME or ~/.codex)",
    )
    parser.add_argument("--output", type=Path, help="New output directory")
    parser.add_argument(
        "--include-incomplete",
        action="store_true",
        help="Include the current incomplete Codex turn in projected outputs",
    )
    parser.add_argument(
        "--allow-missing-assets",
        action="store_true",
        help="Continue when a referenced local attachment cannot be copied",
    )
    parser.add_argument("--verify", type=Path, help="Verify an existing export bundle")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = verify(args.verify) if args.verify else export(args)
    except ExportError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
