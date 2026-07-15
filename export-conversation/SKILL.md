---
name: export-conversation
description: Export complete conversation histories from the current Codex thread, a specified Codex rollout JSONL, or an OpenAI Chat Completions messages JSON file. Use when Codex needs to archive, migrate, back up, verify, or convert a conversation into a byte-preserving source snapshot, a user-visible event log, and portable OpenAI-compatible JSON.
---

# Export Conversation

Use the bundled deterministic exporter. Do not reconstruct conversation history from model context.

## Export

Run:

```bash
python3 <skill-dir>/scripts/export_conversation.py --output <new-output-directory>
```

The script uses `CODEX_THREAD_ID` to locate the current Codex rollout. By default, project visible events only through the last `task_complete`; this excludes the in-progress export invocation while retaining a complete prior conversation.

Use explicit sources when needed:

```bash
python3 <skill-dir>/scripts/export_conversation.py --thread-id <id> --output <directory>
python3 <skill-dir>/scripts/export_conversation.py --input <rollout.jsonl> --output <directory>
python3 <skill-dir>/scripts/export_conversation.py --input <messages.json> --output <directory>
```

Only use `--include-incomplete` when the user explicitly requests an in-progress snapshot. Only use `--allow-missing-assets` when the user accepts a non-self-contained export.

## Verify

After export, always verify the bundle:

```bash
python3 <skill-dir>/scripts/export_conversation.py --verify <output-directory>
```

Report the output directory, conversation ID, completed-only status, message count, warnings, and verification result.

## Output Contract

- `raw/source.jsonl` or `raw/source.json`: byte-preserving source snapshot.
- `raw/completed.jsonl`: Codex JSONL prefix through the selected completion boundary.
- `visible/events.json`: user-visible messages with timestamps, phases, and attachment metadata.
- `openai/messages.json`: portable Chat Completions `messages` projection.
- `assets/`: copied local attachments; OpenAI image messages also contain data URLs.
- `manifest.json`: exporter metadata, source hash, file hashes, cutoff, counts, and warnings.

Treat `raw/` as sensitive because a Codex rollout can contain platform metadata and internal execution records. Share `openai/messages.json` or `visible/events.json` unless the user explicitly requests the raw archive.

## Constraints

- Never choose the newest Codex session heuristically. Require `CODEX_THREAD_ID`, `--thread-id`, or `--input`.
- Never edit or delete the source conversation file.
- Never overwrite an existing output directory.
- Preserve the raw snapshot as the lossless record; the OpenAI projection is intentionally narrower.
- Stop and report an attachment error unless the user allowed missing assets.
