## Harbor Repository Summariser

This repository contains a standalone Python script that connects to a Harbor instance and builds an HTML or Markdown summary of every repository in a Harbor instance.

### Requirements

- Python 3.8+  
- `requests` (`pip install requests`)

### Usage

```bash
python3 generate_harbor_summary.py \
  --base-url https://harbor.example.com \
  --username my-user --output harbor_summary.html
```

Short aliases are available for quicker invocations (for example `-b` for `--base-url`, `-o` for `--output`, `-P` for `--project`, and `-c` for `--column`).

If you omit `--password`, the script securely prompts for it. You can also provide a robot or user API token via `--api-token` instead of username/password. The script writes the summary to the path passed with `--output` (defaults to `harbor_summary.html` for HTML or `harbor_summary.md` for Markdown).

To focus on specific projects, pass one or more `--project` flags (comma-separated values are accepted):

```bash
python3 generate_harbor_summary.py --base-url https://harbor.example.com \
  --api-token "$HARBOR_TOKEN" \
  --project library --project charts
```

To customize the columns shown in the summary table, pass `--column` with one or more column keys (repeat the flag or supply comma-separated values). Use `--list-columns` to see every available key:

```bash
python3 generate_harbor_summary.py --base-url https://harbor.example.com \
  --username my-user --column repository,artifacts --column last_updated
```

To quickly inspect the projects available on the Harbor instance, use `--list-projects` (or `-L`). With an explicit `--output` path the list is saved to a file; otherwise it prints to stdout:

```bash
python3 generate_harbor_summary.py --base-url https://harbor.example.com \
  --api-token "$HARBOR_TOKEN" --list-projects
```

For a Markdown summary instead of HTML, point `--output` to a `.md`/`.markdown` file or pass `--format markdown` (which now defaults to `harbor_summary.md` when no output path is supplied):

```bash
python3 generate_harbor_summary.py --base-url https://harbor.example.com \
  --username my-user --output harbor_summary.md
```

To connect to Harbor instances with self-signed certificates, add `--insecure` to disable TLS verification.

### Full set of options

| Flag(s) | Description | Default |
| ------- | ----------- | ------- |
| `-b`, `--base-url` | Base URL of the Harbor instance. | Required |
| `-u`, `--username` | Harbor username (prompted for password unless `--password` is supplied). | None |
| `-p`, `--password` | Harbor password to pair with `--username`. | Prompted when omitted |
| `-t`, `--api-token` | Harbor robot or user API token (overrides username/password). | None |
| `-k`, `--insecure` | Disable TLS verification (not recommended). | Disabled |
| `-o`, `--output` | File path for the generated summary or project list. | `harbor_summary.html` (HTML) / `harbor_summary.md` (Markdown) |
| `-f`, `--format` | Force `html` or `markdown` output. | Auto-detect from `--output` suffix |
| `-s`, `--page-size` | Page size for Harbor API pagination. | `100` |
| `-T`, `--timeout` | HTTP request timeout in seconds. | `30` |
| `-P`, `--project` | Limit the summary to specific projects (repeatable / comma-separated). | All projects |
| `-c`, `--column` | Limit summary columns (repeatable / comma-separated). | All columns |
| `-l`, `--list-columns` | Print available column keys and exit. | Disabled |
| `-L`, `--list-projects` | List Harbor projects (saved to `--output` if provided). | Prints to stdout |