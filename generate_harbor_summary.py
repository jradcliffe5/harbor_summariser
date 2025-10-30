#!/usr/bin/env python3
"""
Generate an HTML or Markdown summary of all repositories in a Harbor instance using the REST API.

Example:
    python generate_harbor_summary.py --base-url https://harbor.example.com \
        --username admin --output harbor_summary.html
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple
from urllib.parse import urljoin

import requests

# Harbor returns RFC3339 timestamps with a trailing "Z".
ISO_Z_SUFFIX = "Z"
# Use stdlib timezone.utc for compatibility with Python 3.8+.
UTC = timezone.utc

# Default filenames when the user does not specify `--output`.
DEFAULT_HTML_OUTPUT_FILENAME = "harbor_summary.html"
DEFAULT_MARKDOWN_OUTPUT_FILENAME = "harbor_summary.md"


@dataclass
class RepositorySummary:
    name: str
    project_name: str
    pull_count: Optional[int]
    artifact_count: Optional[int]
    update_time: Optional[str]
    description: Optional[str]


@dataclass
class ProjectSummary:
    name: str
    repo_count: int
    repositories: List[RepositorySummary]


@dataclass(frozen=True)
class ColumnDefinition:
    key: str
    label: str
    description: str
    html_renderer: "Callable[[RepositorySummary], str]"
    markdown_renderer: "Callable[[RepositorySummary], str]"


def _render_repository_html(repo: RepositorySummary) -> str:
    """Render repository name as inline `<code>` for HTML output."""
    return f"<code>{escape(repo.name)}</code>"


def _render_artifacts_html(repo: RepositorySummary) -> str:
    """Render artifact count for HTML output, using an em dash when missing."""
    value = "—" if repo.artifact_count is None else str(repo.artifact_count)
    return escape(value)


def _render_pull_count_html(repo: RepositorySummary) -> str:
    """Render pull count for HTML output, using an em dash when missing."""
    value = "—" if repo.pull_count is None else str(repo.pull_count)
    return escape(value)


def _render_last_updated_html(repo: RepositorySummary) -> str:
    """Render last updated timestamp for HTML output."""
    return escape(format_timestamp(repo.update_time))


def _render_description_html(repo: RepositorySummary) -> str:
    """Render repository description for HTML output."""
    description = repo.description.strip() if isinstance(repo.description, str) else repo.description
    if not description:
        description = "—"
    return escape(description)


def _render_repository_markdown(repo: RepositorySummary) -> str:
    """Render repository name as inline code for Markdown output."""
    return f"`{_escape_markdown(repo.name)}`"


def _render_artifacts_markdown(repo: RepositorySummary) -> str:
    """Render artifact count for Markdown output."""
    value = "—" if repo.artifact_count is None else str(repo.artifact_count)
    return _escape_markdown(value)


def _render_pull_count_markdown(repo: RepositorySummary) -> str:
    """Render pull count for Markdown output."""
    value = "—" if repo.pull_count is None else str(repo.pull_count)
    return _escape_markdown(value)


def _render_last_updated_markdown(repo: RepositorySummary) -> str:
    """Render last updated timestamp for Markdown output."""
    return _escape_markdown(format_timestamp(repo.update_time))


def _render_description_markdown(repo: RepositorySummary) -> str:
    """Render repository description for Markdown output."""
    description = repo.description.strip() if isinstance(repo.description, str) else repo.description
    if not description:
        description = "—"
    return _escape_markdown(description)


# Registry describing every column we can show in the summary tables.
COLUMN_DEFINITIONS: Tuple[ColumnDefinition, ...] = (
    ColumnDefinition(
        key="repository",
        label="Repository",
        description="Repository name within the project",
        html_renderer=_render_repository_html,
        markdown_renderer=_render_repository_markdown,
    ),
    ColumnDefinition(
        key="artifacts",
        label="Artifacts",
        description="Number of artifacts stored in the repository",
        html_renderer=_render_artifacts_html,
        markdown_renderer=_render_artifacts_markdown,
    ),
    ColumnDefinition(
        key="pull_count",
        label="Pull Count",
        description="Number of pulls across all artifacts within the repository",
        html_renderer=_render_pull_count_html,
        markdown_renderer=_render_pull_count_markdown,
    ),
    ColumnDefinition(
        key="last_updated",
        label="Last Updated",
        description="Last updated timestamp reported by Harbor",
        html_renderer=_render_last_updated_html,
        markdown_renderer=_render_last_updated_markdown,
    ),
    ColumnDefinition(
        key="description",
        label="Description",
        description="Repository description if available",
        html_renderer=_render_description_html,
        markdown_renderer=_render_description_markdown,
    ),
)

COLUMN_REGISTRY: Dict[str, ColumnDefinition] = {column.key: column for column in COLUMN_DEFINITIONS}


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments for the summary generator."""

    parser = argparse.ArgumentParser(
        description="Generate Harbor repository summaries in HTML or Markdown."
    )
    parser.add_argument(
        "-b",
        "--base-url",
        required=True,
        help="Base URL of the Harbor instance (e.g. https://harbor.example.com).",
    )
    parser.add_argument(
        "-u",
        "--username",
        help="Harbor username. Use along with --password or rely on interactive prompt.",
    )
    parser.add_argument(
        "-p",
        "--password",
        help="Harbor password. If omitted while --username is set, an interactive prompt is used.",
    )
    parser.add_argument(
        "-t",
        "--api-token",
        help=(
            "Harbor robot or user API token. If provided, it is sent as a Bearer token "
            "and takes precedence over username/password."
        ),
    )
    parser.add_argument(
        "-k",
        "--insecure",
        action="store_true",
        help="Disable TLS verification (not recommended).",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help=(
            "Path to write the generated summary file (defaults to harbor_summary.html, "
            "or harbor_summary.md when --format markdown)."
        ),
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("html", "markdown"),
        default=None,
        help="Output format for the summary. Defaults to HTML unless the output filename ends with .md/.markdown.",
    )
    parser.add_argument(
        "-s",
        "--page-size",
        type=int,
        default=100,
        help="Number of items to fetch per API page when listing projects and repositories.",
    )
    parser.add_argument(
        "-T",
        "--timeout",
        type=float,
        default=30.0,
        help="HTTP timeout in seconds for API calls.",
    )
    parser.add_argument(
        "-P",
        "--project",
        dest="projects",
        action="append",
        help=(
            "Limit the summary to one or more projects. Repeat this flag or provide a comma-separated list."
        ),
    )
    parser.add_argument(
        "-c",
        "--column",
        dest="columns",
        action="append",
        help=(
            "Restrict the summary table to specific columns. Repeat this flag or provide a comma-separated list."
        ),
    )
    parser.add_argument(
        "-l",
        "--list-columns",
        action="store_true",
        help="Print the available column keys and exit.",
    )
    parser.add_argument(
        "-L",
        "--list-projects",
        action="store_true",
        help="List Harbor projects (with repository counts) and exit.",
    )

    args = parser.parse_args()
    args.explicit_output = args.output is not None
    if args.output is None:
        args.output = DEFAULT_HTML_OUTPUT_FILENAME
    return args


def ensure_credentials(args: argparse.Namespace) -> None:
    """Prompt for or validate credentials based on argparse results."""
    if args.api_token:
        return
    if not args.username:
        raise SystemExit("Error: --username or --api-token is required.")
    if args.password is None:
        args.password = getpass.getpass("Harbor password: ")


def build_session(args: argparse.Namespace) -> requests.Session:
    """Create a configured `requests.Session` for interacting with Harbor."""
    session = requests.Session()
    session.verify = not args.insecure
    session.headers.update({"Accept": "application/json"})
    if args.api_token:
        session.headers["Authorization"] = f"Bearer {args.api_token}"
    else:
        session.auth = (args.username, args.password)
    return session


def fetch_paginated(
    session: requests.Session,
    base_url: str,
    path: str,
    *,
    page_size: int,
    extra_headers: Optional[Dict[str, str]] = None,
    params: Optional[Mapping[str, Any]] = None,
    timeout: float,
) -> Iterable[Dict[str, Any]]:
    """Yield dictionaries from a paginated Harbor API endpoint."""
    page = 1
    while True:
        query: Dict[str, Any] = {"page": page, "page_size": page_size}
        if params:
            query.update(params)
        response = session.get(
            urljoin(base_url, path),
            params=query,
            headers=extra_headers,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, list):
            raise ValueError(
                f"Unexpected response for {path}: {json.dumps(data, indent=2)[:200]}..."
            )
        if not data:
            break
        for item in data:
            if not isinstance(item, dict):
                continue
            yield item
        if len(data) < page_size:
            break
        page += 1


def format_timestamp(value: Optional[str]) -> str:
    """Convert an ISO timestamp to a human-readable UTC string."""
    if not value:
        return "—"
    try:
        cleaned = value.replace(ISO_Z_SUFFIX, "+00:00") if value.endswith(ISO_Z_SUFFIX) else value
        dt = datetime.fromisoformat(cleaned)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return value


def build_html(projects: List[ProjectSummary], columns: List[ColumnDefinition]) -> str:
    """Render the collected project data as an HTML document."""
    total_projects = len(projects)
    total_repositories = sum(len(project.repositories) for project in projects)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    rows: List[str] = [
        "<!DOCTYPE html>",
        "<html lang='en'>",
        "<head>",
        "<meta charset='utf-8' />",
        "<title>Harbor Repository Summary</title>",
        "<style>",
        "body { font-family: Arial, sans-serif; margin: 2rem; background: #f9fafc; color: #172b4d; }",
        "h1 { margin-bottom: 0.25rem; }",
        "section { margin-top: 2rem; }",
        "table { border-collapse: collapse; width: 100%; margin-top: 1rem; }",
        "th, td { border: 1px solid #dfe1e6; padding: 0.5rem 0.75rem; text-align: left; }",
        "th { background-color: #f4f5f7; }",
        "tbody tr:nth-child(even) { background-color: #f8f9fc; }",
        "code { background: #f4f5f7; padding: 0.125rem 0.25rem; border-radius: 4px; }",
        "footer { margin-top: 4rem; font-size: 0.875rem; color: #6b778c; }",
        "</style>",
        "</head>",
        "<body>",
        "<h1>Harbor Repository Summary</h1>",
        f"<p>Generated at {escape(timestamp)} · {total_projects} projects · {total_repositories} repositories.</p>",
    ]

    for project in sorted(projects, key=lambda p: p.name.lower()):
        rows.append("<section>")
        rows.append(f"<h2>Project: {escape(project.name)} ({project.repo_count} repositories)</h2>")
        if not project.repositories:
            rows.append("<p>No repositories available.</p>")
        else:
            rows.append("<table>")
            header_cells = "".join(f"<th>{escape(column.label)}</th>" for column in columns)
            rows.append(f"<thead><tr>{header_cells}</tr></thead>")
            rows.append("<tbody>")
            for repo in sorted(project.repositories, key=lambda r: r.name.lower()):
                cell_html = "".join(f"<td>{column.html_renderer(repo)}</td>" for column in columns)
                rows.append(f"<tr>{cell_html}</tr>")
            rows.append("</tbody>")
            rows.append("</table>")
        rows.append("</section>")

    rows.extend(
        [
            "<footer>",
            "<p>Generated by generate_harbor_summary.py.</p>",
            "</footer>",
            "</body>",
            "</html>",
        ]
    )
    return "\n".join(rows)


def build_markdown(projects: List[ProjectSummary], columns: List[ColumnDefinition]) -> str:
    """Render the collected project data as a Markdown document."""
    total_projects = len(projects)
    total_repositories = sum(len(project.repositories) for project in projects)
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    lines: List[str] = [
        "# Harbor Repository Summary",
        "",
        f"Generated at {timestamp} · {total_projects} projects · {total_repositories} repositories.",
        "",
    ]

    for project in sorted(projects, key=lambda p: p.name.lower()):
        lines.append(f"## Project: {_escape_markdown(project.name)} ({project.repo_count} repositories)")
        lines.append("")
        if not project.repositories:
            lines.append("_No repositories available._")
            lines.append("")
            continue
        header = " | ".join(_escape_markdown(column.label) for column in columns)
        separator = " | ".join("---" for _ in columns)
        lines.append(f"| {header} |")
        lines.append(f"| {separator} |")
        for repo in sorted(project.repositories, key=lambda r: r.name.lower()):
            row = " | ".join(column.markdown_renderer(repo) for column in columns)
            lines.append(f"| {row} |")
        lines.append("")

    lines.append("_Generated by generate_harbor_summary.py_")
    lines.append("")
    return "\n".join(lines)


def collect_data(args: argparse.Namespace) -> List[ProjectSummary]:
    """Fetch projects and repositories from Harbor, applying any filters."""
    ensure_credentials(args)
    session = build_session(args)
    timeout = args.timeout
    projects: List[ProjectSummary] = []
    project_filters, filter_lookup = _prepare_project_filters(getattr(args, "projects", None))
    remaining_filters = set(project_filters) if project_filters else set()

    for project in fetch_paginated(
        session,
        args.base_url,
        "/api/v2.0/projects",
        page_size=args.page_size,
        timeout=timeout,
    ):
        name = str(project.get("name", ""))
        if not name:
            continue
        normalized_name = name.lower()
        if project_filters and normalized_name not in project_filters:
            # Skip projects outside the requested subset.
            continue
        remaining_filters.discard(normalized_name)
        repo_count = int(project.get("repo_count", 0) or 0)
        repositories: List[RepositorySummary] = []
        for repo in fetch_paginated(
            session,
            args.base_url,
            f"/api/v2.0/projects/{name}/repositories",
            page_size=args.page_size,
            timeout=timeout,
            extra_headers={"X-Is-Resource-Name": "true"},
        ):
            repositories.append(
                RepositorySummary(
                    name=str(repo.get("name", "")),
                    project_name=name,
                    pull_count=_safe_int(repo.get("pull_count")),
                    artifact_count=_safe_int(repo.get("artifact_count")),
                    update_time=repo.get("update_time"),
                    description=repo.get("description"),
                )
            )
        projects.append(ProjectSummary(name=name, repo_count=repo_count, repositories=repositories))

    if remaining_filters:
        missing = ", ".join(sorted(filter_lookup[key] for key in remaining_filters))
        print(f"Warning: requested projects not found: {missing}", file=sys.stderr)

    return projects


def _safe_int(value: Any) -> Optional[int]:
    """Attempt to coerce `value` to an int, returning `None` when that fails."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _escape_markdown(value: Any) -> str:
    """Escape characters that would break Markdown table formatting."""
    text = str(value)
    text = text.replace("\\", "\\\\")
    text = text.replace("|", "\\|")
    text = text.replace("`", "\\`")
    text = text.replace("\n", "<br />")
    return text


def _prepare_columns(
    raw_columns: Optional[List[str]],
) -> List[ColumnDefinition]:
    """Resolve the requested columns into `ColumnDefinition` objects."""
    if not raw_columns:
        return list(COLUMN_DEFINITIONS)
    tokens: List[str] = []
    for raw_value in raw_columns:
        if raw_value is None:
            continue
        for token in raw_value.split(","):
            normalized = token.strip().lower()
            if normalized:
                tokens.append(normalized)
    if raw_columns and not tokens:
        raise SystemExit("No valid columns specified via --column.")

    resolved: List[ColumnDefinition] = []
    seen: Set[str] = set()
    for key in tokens:
        if key in seen:
            continue
        column = COLUMN_REGISTRY.get(key)
        if column is None:
            raise SystemExit(f"Unknown column '{key}'. Use --list-columns to view available columns.")
        seen.add(key)
        resolved.append(column)
    if not resolved:
        return list(COLUMN_DEFINITIONS)
    return resolved


def _print_available_columns() -> None:
    """Display every column key, label, and description."""
    print("Available columns:")
    for column in COLUMN_DEFINITIONS:
        print(f"- {column.key}: {column.label} — {column.description}")


def _prepare_project_filters(
    raw_filters: Optional[List[str]],
) -> Tuple[Optional[Set[str]], Dict[str, str]]:
    """Normalize requested project filters for consistent comparisons."""
    if not raw_filters:
        return None, {}
    mapping: Dict[str, str] = {}
    for raw_value in raw_filters:
        if raw_value is None:
            continue
        for token in raw_value.split(","):
            cleaned = token.strip()
            if not cleaned:
                continue
            mapping[cleaned.lower()] = cleaned
    if not mapping:
        return None, {}
    return set(mapping.keys()), mapping


def _list_projects(args: argparse.Namespace) -> None:
    """List Harbor projects (with repo counts) to stdout or an optional file."""
    ensure_credentials(args)
    session = build_session(args)
    project_filters, filter_lookup = _prepare_project_filters(getattr(args, "projects", None))
    remaining_filters = set(project_filters) if project_filters else set()

    projects: List[Tuple[str, int]] = []
    for project in fetch_paginated(
        session,
        args.base_url,
        "/api/v2.0/projects",
        page_size=args.page_size,
        timeout=args.timeout,
    ):
        name = str(project.get("name", ""))
        if not name:
            continue
        normalized_name = name.lower()
        if project_filters and normalized_name not in project_filters:
            continue
        remaining_filters.discard(normalized_name)
        repo_count = int(project.get("repo_count", 0) or 0)
        projects.append((name, repo_count))

    if remaining_filters:
        missing = ", ".join(sorted(filter_lookup[key] for key in remaining_filters))
        print(f"Warning: requested projects not found: {missing}", file=sys.stderr)

    if not projects:
        output_text = "No projects found."
    else:
        projects.sort(key=lambda item: item[0].lower())
        output_text = "\n".join(f"{name} ({count} repositories)" for name, count in projects)

    if getattr(args, "explicit_output", False):
        output_path = Path(args.output)
        output_path.write_text(output_text + "\n", encoding="utf-8")
        print(f"Wrote project list to {output_path.resolve()}")
    else:
        print(output_text)


def _infer_output_format(output_path: str) -> str:
    """Infer summary output format based on the output path extension."""
    suffix = Path(output_path).suffix.lower()
    if suffix in {".md", ".markdown"}:
        return "markdown"
    return "html"


def main() -> None:
    """Entrypoint for generating Harbor repository summaries."""
    args = parse_args()
    if getattr(args, "list_columns", False):
        _print_available_columns()
        return
    if getattr(args, "list_projects", False):
        _list_projects(args)
        return

    output_format = args.format or _infer_output_format(args.output)
    if not args.explicit_output:
        args.output = (
            DEFAULT_MARKDOWN_OUTPUT_FILENAME
            if output_format == "markdown"
            else DEFAULT_HTML_OUTPUT_FILENAME
        )
    columns = _prepare_columns(getattr(args, "columns", None))
    try:
        projects = collect_data(args)
    except requests.HTTPError as exc:
        response = exc.response
        details = f"[{response.status_code}] {response.text}" if response is not None else str(exc)
        raise SystemExit(f"Harbor API error: {details}")
    except requests.RequestException as exc:
        raise SystemExit(f"Network error while contacting Harbor: {exc}") from exc

    if output_format == "markdown":
        summary = build_markdown(projects, columns)
        label = "Markdown"
    else:
        summary = build_html(projects, columns)
        label = "HTML"
    output_path = Path(args.output)
    output_path.write_text(summary, encoding="utf-8")
    print(f"Wrote {label} summary to {output_path.resolve()}")


if __name__ == "__main__":
    main()
