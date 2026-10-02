# Deduperall

Find duplicate documents, review them in a browser UI, and move the extras to a quarantine folder. **Nothing is ever deleted**, and one click puts everything back.

Single file. Python 3.8+ standard library only. No installs.

## Run

```bash
python3 dedupe_office.py              # opens the UI at http://127.0.0.1:8765
python3 dedupe_office.py --cli        # dry-run report in the terminal
python3 dedupe_office.py --cli --move # quarantine the suggested duplicates
```

Options: `--root DIR` (repeatable), `--protect TEXT` (skip any path containing it, repeatable), `--quarantine DIR`, `--port N`.

## How it works

- Matches by **size, then SHA-256**, so only byte-identical files count as duplicates. Empty files are ignored.
- Suggests which copy to keep: clean filename (no "copy", "(1)", "final", "backup"), then shallowest path, then oldest.
- Moves duplicates to `~/dupe_quarantine` and records the original path and hash in `manifest.json`. **Restore** puts every file back (it won't overwrite anything already at the original path).
- Default types: doc/docx, xls/xlsx, ppt/pptx, odt/ods/odp, rtf, txt, pdf.

## Safety

- The server binds to `127.0.0.1` only, and every API call needs a per-run token.
- It will only move files found by the most recent scan.
- Use the **Protect** box (or `--protect`) for folders where copies matter, such as evidence or records you must keep as-is.

## Local API

For scripts or an agent skill (token is printed at startup):

```
POST /api/scan     {"roots": [...], "protect": [...], "exts": [".docx", ...]}
POST /api/move     {"paths": [...], "quarantine": "..."}
POST /api/restore  {"quarantine": "..."}
```
Send the token in an `X-Token` header.

## Limits

Exact-hash matching only. A file that was re-saved or edited, even slightly, is a different file and won't be flagged.

## License

MIT
