# Tool Usage Notes

Tool signatures are provided automatically via function calling.
This file documents non-obvious constraints and usage patterns.

## Binary & Attachments — Hard Rules

- No fabrication: never generate any non-text/binary content (images/audio/video/pdf/zip), nor large base64/hex blobs.
- No guessing: never reconstruct, “fix”, or complete truncated binary payloads.
- No raw forwarding: never forward inline payloads like `type='image' data='...'`.
- Only by tools/scripts: always save external binary payloads to a real file via tools/scripts, then verify and send.
- Only from workspace: attachments must come from `mcp_media/` or `attachments/` under the workspace (verify non-empty).

## exec — Safety Limits

- Commands have a configurable timeout (default 60s)
- Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)
- Output is truncated at 10,000 characters
- `restrictToWorkspace` config can limit file access to the workspace

## cron — Scheduled Reminders

- Please refer to cron skill for usage.
