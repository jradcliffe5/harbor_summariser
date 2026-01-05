## Harbor Repository Summariser

This repository contains a standalone Python script that connects to a Harbor instance and builds an HTML, Markdown, or Confluence storage-format summary of every repository in a Harbor instance.

### Requirements

- Python 3.8+  
- `requests` (`pip install requests`)
- `singularity` / `apptainer` (optional; only needed when using `--pull-dir` to pull images)

### Usage

```bash
python3 generate_harbor_summary.py \
  --base-url https://harbor.example.com \
  --username my-user --output harbor_summary.html
```

To summarize multiple Harbor instances in a single run, repeat `--instance` with a base URL and credentials for each registry:

```bash
python3 generate_harbor_summary.py \
  --instance https://harbor-one.example.com,api-token="$HARBOR_TOKEN_ONE" \
  --instance base-url=https://harbor-two.example.com,api-token="$HARBOR_TOKEN_TWO" \
  --format markdown --output multi_harbor.md
```
Per-instance credentials take precedence; when omitted, the script falls back to the global `--api-token` or `--username`/`--password` flags.

To download every summarised repository as Singularity/Apptainer images, add `--pull-dir` to choose a destination directory (uses the `oras` transport by default):

```bash
python3 generate_harbor_summary.py \
  --instance https://harbor.example.com,username=robot$user,api-token="$HARBOR_TOKEN" \
  --pull-dir ./pulled_images --pull-transport oras --singularity-bin apptainer
```
Provide a username along with your token when pulls require authentication.

Short aliases are available for quicker invocations (for example `-B` for `--instance`, `-b` for `--base-url`, `-o` for `--output`, `-P` for `--project`, and `-c` for `--column`).

If you omit `--password`, the script securely prompts for it. You can also provide a robot or user API token via `--api-token` instead of username/password. The script writes the summary to the path passed with `--output` (defaults to `harbor_summary.html` for HTML, `harbor_summary.md` for Markdown, or `harbor_summary_confluence.xml` for Confluence storage format).

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

For a Confluence storage-format summary (suitable for pasting into the Confluence source editor or sending as `storage` content via the Confluence REST API), point `--output` to `.xml`/`.confluence` or pass `--format confluence`:

```bash
python3 generate_harbor_summary.py --base-url https://harbor.example.com \
  --api-token "$HARBOR_TOKEN" --format confluence --output harbor_summary_confluence.xml
```

To connect to Harbor instances with self-signed certificates, add `--insecure` to disable TLS verification.

### Full set of options

| Flag(s) | Description | Default |
| ------- | ----------- | ------- |
| `-B`, `--instance` | Add a Harbor instance in the form `BASE_URL[,api-token=TOKEN][,username=USER][,password=PASS]` (repeatable). | None |
| `-b`, `--base-url` | Base URL of the Harbor instance (required unless `--instance` is used). | None |
| `-u`, `--username` | Harbor username (prompted for password unless `--password` is supplied). | None |
| `-p`, `--password` | Harbor password to pair with `--username`. | Prompted when omitted |
| `-t`, `--api-token` | Harbor robot or user API token (overrides username/password). | None |
| `-k`, `--insecure` | Disable TLS verification (not recommended). | Disabled |
| `-o`, `--output` | File path for the generated summary or project list. | `harbor_summary.html` (HTML) / `harbor_summary.md` (Markdown) / `harbor_summary_confluence.xml` (Confluence storage) |
| `--pull-dir` | Directory where Singularity/Apptainer pulls of all summarised repositories are saved as `.sif`. | None |
| `--pull-transport` | Transport for pulls (`oras` or `docker`). | `oras` |
| `--singularity-bin` | Singularity/Apptainer executable to run for pulls. | `singularity` |
| `-f`, `--format` | Force `html`, `markdown`, or `confluence` output. | Auto-detect from `--output` suffix (`.md`/`.markdown` → Markdown, `.xml`/`.confluence` → Confluence, otherwise HTML) |
| `-s`, `--page-size` | Page size for Harbor API pagination. | `100` |
| `-T`, `--timeout` | HTTP request timeout in seconds. | `30` |
| `-P`, `--project` | Limit the summary to specific projects (repeatable / comma-separated). | All projects |
| `-c`, `--column` | Limit summary columns (repeatable / comma-separated). | All columns |
| `-l`, `--list-columns` | Print available column keys and exit. | Disabled |
| `-L`, `--list-projects` | List Harbor projects (saved to `--output` if provided). | Prints to stdout |
