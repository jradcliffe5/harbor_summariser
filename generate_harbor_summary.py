#!/usr/bin/env python3
"""
Generate an HTML, Markdown, or Confluence storage-format summary of all repositories in a Harbor
instance using the REST API.

Example:
    python generate_harbor_summary.py --base-url https://harbor.example.com \
        --username admin --output harbor_summary.html
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Set, Tuple
from urllib.parse import quote, urljoin, urlparse

import requests

# Harbor returns RFC3339 timestamps with a trailing "Z".
ISO_Z_SUFFIX = "Z"
# Use stdlib timezone.utc for compatibility with Python 3.8+.
UTC = timezone.utc

# Default filenames when the user does not specify `--output`.
DEFAULT_HTML_OUTPUT_FILENAME = "harbor_summary.html"
DEFAULT_MARKDOWN_OUTPUT_FILENAME = "harbor_summary.md"
DEFAULT_CONFLUENCE_OUTPUT_FILENAME = "harbor_summary_confluence.xml"


@dataclass
class ArtifactSummary:
    digest: Optional[str]
    tags: List[str]


@dataclass
class RepositorySummary:
    name: str
    project_name: str
    pull_count: Optional[int]
    artifact_count: Optional[int]
    update_time: Optional[str]
    description: Optional[str]
    artifacts: List[ArtifactSummary] = field(default_factory=list)


@dataclass
class ProjectSummary:
    name: str
    repo_count: int
    repositories: List[RepositorySummary]


@dataclass
class HarborInstanceConfig:
    base_url: str
    username: Optional[str]
    password: Optional[str]
    api_token: Optional[str]
    projects: Optional[List[str]] = None


@dataclass
class HarborInstanceSummary:
    base_url: str
    projects: List[ProjectSummary]
    username: Optional[str] = None
    password: Optional[str] = None
    api_token: Optional[str] = None
    project_filters: Optional[List[str]] = None


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
        description="Generate Harbor repository summaries in HTML, Markdown, or Confluence storage format."
    )
    parser.add_argument(
        "-B",
        "--instance",
        dest="instances",
        action="append",
        metavar="BASE_URL[,api-token=TOKEN][,username=USER][,password=PASS]",
        help=(
            "Connect to an additional Harbor instance. Repeat this flag to summarize multiple Harbor instances "
            "in a single run. Per-instance credentials override the global username/password or api-token flags."
        ),
    )
    parser.add_argument(
        "-b",
        "--base-url",
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
            "harbor_summary.md when --format markdown, or harbor_summary_confluence.xml when "
            "--format confluence)."
        ),
    )
    parser.add_argument(
        "--pull-dir",
        metavar="DIR",
        help=(
            "Optional directory where Singularity/Apptainer pulls of all summarised repositories will be saved "
            "as .sif files."
        ),
    )
    parser.add_argument(
        "--pull-transport",
        choices=("oras", "docker"),
        default="oras",
        help="Transport to use when pulling images (oras is recommended for SIF stored in Harbor).",
    )
    parser.add_argument(
        "--pull-fallback",
        action="store_true",
        help="Retry failed pulls with the opposite transport (oras↔docker).",
    )
    parser.add_argument(
        "--pull-overwrite",
        action="store_true",
        help="Overwrite existing pulled images instead of skipping them.",
    )
    parser.add_argument(
        "--singularity-bin",
        default="singularity",
        help="Executable to use for pulling images (e.g. singularity or apptainer).",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("html", "markdown", "confluence"),
        default=None,
        help=(
            "Output format for the summary. Defaults to HTML unless the output filename ends with "
            ".md/.markdown (Markdown) or .xml/.confluence (Confluence storage format)."
        ),
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
    if not args.base_url and not getattr(args, "instances", None):
        parser.error("--base-url is required unless at least one --instance is provided.")
    args.explicit_output = args.output is not None
    if args.output is None:
        args.output = DEFAULT_HTML_OUTPUT_FILENAME
    return args


def _parse_instance_spec(raw_value: str) -> HarborInstanceConfig:
    """Parse a single --instance flag value into a HarborInstanceConfig."""
    if not isinstance(raw_value, str) or not raw_value.strip():
        raise SystemExit("Each --instance requires a base URL and optional credentials.")

    parts = [part.strip() for part in raw_value.split(",") if part.strip()]
    base_url: Optional[str] = None
    values: Dict[str, str] = {}
    projects: List[str] = []

    for index, part in enumerate(parts):
        if "=" in part:
            key, value = part.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if not key:
                raise SystemExit(f"Invalid --instance segment '{part}'. Expected key=value pairs.")
            if key in {"project", "projects"}:
                tokens = [token.strip() for token in re.split(r"[|;]", value) if token.strip()]
                if tokens:
                    projects.extend(tokens)
                else:
                    projects.append(value) if value else None
                continue
            values[key] = value
        elif index == 0 and base_url is None:
            base_url = part
        else:
            raise SystemExit(
                f"Invalid --instance segment '{part}'. Provide key=value pairs after the base URL."
            )

    base_url = values.pop("base-url", base_url)
    api_token = values.pop("api-token", None)
    username = values.pop("username", None)
    password = values.pop("password", None)
    if values:
        unknown_keys = ", ".join(sorted(values.keys()))
        raise SystemExit(f"Unknown --instance keys: {unknown_keys}")
    if not base_url:
        raise SystemExit(
            "Each --instance must include a base URL (for example "
            "--instance https://harbor.example.com,api-token=TOKEN)."
        )

    return HarborInstanceConfig(
        base_url=base_url,
        username=username,
        password=password,
        api_token=api_token,
        projects=projects or None,
    )


def _prepare_instances(args: argparse.Namespace) -> List[HarborInstanceConfig]:
    """Build a list of HarborInstanceConfig objects from CLI args."""
    instances: List[HarborInstanceConfig] = []
    for raw_value in getattr(args, "instances", []) or []:
        instances.append(_parse_instance_spec(raw_value))
    if args.base_url:
        instances.append(
            HarborInstanceConfig(
                base_url=args.base_url,
                username=args.username,
                password=args.password,
                api_token=args.api_token,
                projects=None,
            )
        )
    if not instances:
        raise SystemExit("Error: at least one Harbor instance must be provided.")
    return instances


def ensure_instance_credentials(instance: HarborInstanceConfig, defaults: argparse.Namespace) -> None:
    """Prompt for or validate credentials for a specific Harbor instance."""
    if instance.api_token:
        return
    if instance.username:
        if instance.password is None:
            instance.password = getpass.getpass(
                f"Harbor password for {instance.username} @ {instance.base_url}: "
            )
            if defaults.username and instance.username == defaults.username and defaults.password is None:
                defaults.password = instance.password
        return
    if defaults.api_token:
        instance.api_token = defaults.api_token
        return
    if defaults.username:
        instance.username = defaults.username
        instance.password = defaults.password
        if instance.password is None:
            instance.password = getpass.getpass(
                f"Harbor password for {instance.username} @ {instance.base_url}: "
            )
            defaults.password = instance.password
        return
    raise SystemExit(
        f"Error: credentials required for {instance.base_url}. Provide an api-token or username/password."
    )


def build_session(instance: HarborInstanceConfig, insecure: bool) -> requests.Session:
    """Create a configured `requests.Session` for interacting with Harbor."""
    session = requests.Session()
    session.verify = not insecure
    session.headers.update({"Accept": "application/json"})
    if instance.api_token:
        session.headers["Authorization"] = f"Bearer {instance.api_token}"
    else:
        session.auth = (instance.username, instance.password)
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


def _strip_project_prefix(repo_name: str, project_name: str) -> str:
    """Remove the leading project prefix from a repository path when present."""
    prefix = f"{project_name}/"
    if repo_name.startswith(prefix):
        return repo_name[len(prefix) :]
    return repo_name


def _fetch_artifacts_for_repository(
    session: requests.Session,
    base_url: str,
    project_name: str,
    repository_name: str,
    *,
    page_size: int,
    timeout: float,
) -> List[ArtifactSummary]:
    """Fetch all artifacts (with tags) for a specific repository."""
    artifacts: List[ArtifactSummary] = []
    repository_segment = quote(_strip_project_prefix(repository_name, project_name), safe="")
    for artifact in fetch_paginated(
        session,
        base_url,
        f"/api/v2.0/projects/{project_name}/repositories/{repository_segment}/artifacts",
        page_size=page_size,
        timeout=timeout,
        extra_headers={"X-Is-Resource-Name": "true"},
    ):
        tags: List[str] = []
        for tag in artifact.get("tags") or []:
            tag_name = tag.get("name")
            if tag_name:
                tags.append(str(tag_name))
        digest = artifact.get("digest")
        artifacts.append(ArtifactSummary(digest=str(digest) if digest else None, tags=tags))
    return artifacts


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


def build_html(instances: List[HarborInstanceSummary], columns: List[ColumnDefinition]) -> str:
    """Render the collected project data as an HTML document."""
    total_projects = sum(len(instance.projects) for instance in instances)
    total_repositories = sum(
        len(project.repositories) for instance in instances for project in instance.projects
    )
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
        (
            f"<p>Generated at {escape(timestamp)} · {total_projects} projects · "
            f"{total_repositories} repositories across {len(instances)} Harbor instance(s).</p>"
        ),
    ]

    for instance in sorted(instances, key=lambda inst: inst.base_url.lower()):
        rows.append("<section>")
        rows.append(f"<h2>Harbor: {escape(instance.base_url)}</h2>")
        if not instance.projects:
            rows.append("<p>No projects available.</p>")
        for project in sorted(instance.projects, key=lambda p: p.name.lower()):
            rows.append(f"<h3>Project: {escape(project.name)} ({project.repo_count} repositories)</h3>")
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


def build_markdown(instances: List[HarborInstanceSummary], columns: List[ColumnDefinition]) -> str:
    """Render the collected project data as a Markdown document."""
    total_projects = sum(len(instance.projects) for instance in instances)
    total_repositories = sum(
        len(project.repositories) for instance in instances for project in instance.projects
    )
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    lines: List[str] = [
        "# Harbor Repository Summary",
        "",
        (
            f"Generated at {timestamp} · {total_projects} projects · "
            f"{total_repositories} repositories across {len(instances)} Harbor instance(s)."
        ),
        "",
    ]

    for instance in sorted(instances, key=lambda inst: inst.base_url.lower()):
        lines.append(f"## Harbor: {_escape_markdown(instance.base_url)}")
        lines.append("")
        if not instance.projects:
            lines.append("_No projects available._")
            lines.append("")
            continue
        for project in sorted(instance.projects, key=lambda p: p.name.lower()):
            lines.append(
                f"### Project: {_escape_markdown(project.name)} ({project.repo_count} repositories)"
            )
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


def build_confluence_storage(
    instances: List[HarborInstanceSummary], columns: List[ColumnDefinition]
) -> str:
    """Render the project data as Confluence storage-format (XHTML) markup."""
    total_projects = sum(len(instance.projects) for instance in instances)
    total_repositories = sum(
        len(project.repositories) for instance in instances for project in instance.projects
    )
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    parts: List[str] = [
        "<h1>Harbor Repository Summary</h1>",
        (
            f"<p>Generated at {escape(timestamp)} · {total_projects} projects · "
            f"{total_repositories} repositories across {len(instances)} Harbor instance(s).</p>"
        ),
    ]

    for instance in sorted(instances, key=lambda inst: inst.base_url.lower()):
        parts.append(f"<h2>Harbor: {escape(instance.base_url)}</h2>")
        if not instance.projects:
            parts.append("<p><em>No projects available.</em></p>")
            continue
        for project in sorted(instance.projects, key=lambda p: p.name.lower()):
            parts.append(f"<h3>Project: {escape(project.name)} ({project.repo_count} repositories)</h3>")
            if not project.repositories:
                parts.append("<p><em>No repositories available.</em></p>")
                continue
            header_cells = "".join(f"<th>{escape(column.label)}</th>" for column in columns)
            parts.append("<table>")
            parts.append(f"<thead><tr>{header_cells}</tr></thead>")
            parts.append("<tbody>")
            for repo in sorted(project.repositories, key=lambda r: r.name.lower()):
                cell_html = "".join(f"<td>{column.html_renderer(repo)}</td>" for column in columns)
                parts.append(f"<tr>{cell_html}</tr>")
            parts.append("</tbody>")
            parts.append("</table>")

    parts.append("<p><em>Generated by generate_harbor_summary.py.</em></p>")
    return "\n".join(parts)


def collect_data(args: argparse.Namespace) -> List[HarborInstanceSummary]:
    """Fetch projects and repositories from one or more Harbor instances."""
    instances = _prepare_instances(args)
    global_filters, global_lookup = _prepare_project_filters(getattr(args, "projects", None))
    collect_artifacts = bool(getattr(args, "pull_dir", None))

    all_projects: List[HarborInstanceSummary] = []

    for instance in instances:
        instance_filters, instance_lookup = _prepare_project_filters(instance.projects)
        effective_filters = instance_filters if instance_filters is not None else global_filters
        effective_lookup = instance_lookup if instance_lookup else global_lookup
        remaining_filters = set(effective_filters) if effective_filters else set()

        ensure_instance_credentials(instance, args)
        session = build_session(instance, insecure=args.insecure)
        timeout = args.timeout
        projects: List[ProjectSummary] = []

        for project in fetch_paginated(
            session,
            instance.base_url,
            "/api/v2.0/projects",
            page_size=args.page_size,
            timeout=timeout,
        ):
            name = str(project.get("name", ""))
            if not name:
                continue
            normalized_name = name.lower()
            if effective_filters and normalized_name not in effective_filters:
                # Skip projects outside the requested subset.
                continue
            remaining_filters.discard(normalized_name)
            repo_count = int(project.get("repo_count", 0) or 0)
            repositories: List[RepositorySummary] = []
            for repo in fetch_paginated(
                session,
                instance.base_url,
                f"/api/v2.0/projects/{name}/repositories",
                page_size=args.page_size,
                timeout=timeout,
                extra_headers={"X-Is-Resource-Name": "true"},
            ):
                artifacts: List[ArtifactSummary] = []
                repo_name = str(repo.get("name", ""))
                if collect_artifacts and repo_name:
                    artifacts = _fetch_artifacts_for_repository(
                        session,
                        instance.base_url,
                        project_name=name,
                        repository_name=repo_name,
                        page_size=args.page_size,
                        timeout=timeout,
                    )
                repositories.append(
                    RepositorySummary(
                        name=repo_name,
                        project_name=name,
                        pull_count=_safe_int(repo.get("pull_count")),
                        artifact_count=_safe_int(repo.get("artifact_count")),
                        update_time=repo.get("update_time"),
                        description=repo.get("description"),
                        artifacts=artifacts,
                    )
                )
            projects.append(ProjectSummary(name=name, repo_count=repo_count, repositories=repositories))
        if remaining_filters:
            missing = ", ".join(sorted(effective_lookup[key] for key in remaining_filters))
            print(f"Warning: requested projects not found in {instance.base_url}: {missing}", file=sys.stderr)
        all_projects.append(
            HarborInstanceSummary(
                base_url=instance.base_url,
                projects=projects,
                username=instance.username,
                password=instance.password,
                api_token=instance.api_token,
                project_filters=instance.projects,
            )
        )

    return all_projects


def _safe_int(value: Any) -> Optional[int]:
    """Attempt to coerce `value` to an int, returning `None` when that fails."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _sanitize_for_fs(value: str, fallback: str = "item") -> str:
    """Sanitize a string so it is safe to use as a filename or directory."""
    cleaned = re.sub(r'[\\/:*?"<>|]+', "_", str(value)).strip()
    cleaned = cleaned.replace("@", "_").replace(" ", "_")
    return cleaned or fallback


def _registry_host_from_base_url(base_url: str) -> str:
    """Extract the registry host (and optional port) from a Harbor base URL."""
    parsed = urlparse(base_url)
    if parsed.scheme:
        host = parsed.netloc or parsed.path
    else:
        host = base_url
    return host.rstrip("/")


def pull_singularity_images(
    instances: List[HarborInstanceSummary],
    *,
    pull_dir: str,
    transport: str,
    singularity_bin: str,
    overwrite: bool,
    fallback_opposite: bool,
) -> None:
    """Pull all discovered artifacts as Singularity/Apptainer images into a directory."""
    if not pull_dir:
        return

    binary_path = shutil.which(singularity_bin)
    if binary_path is None:
        raise SystemExit(
            f"Unable to find '{singularity_bin}'. Install Singularity/Apptainer or set --singularity-bin."
        )

    destination_root = Path(pull_dir)
    destination_root.mkdir(parents=True, exist_ok=True)
    base_env = os.environ.copy()

    for instance in instances:
        registry_host = _registry_host_from_base_url(instance.base_url)
        if not registry_host:
            registry_host = _sanitize_for_fs(instance.base_url, "harbor")
        instance_dir = destination_root / _sanitize_for_fs(registry_host, "harbor")
        instance_dir.mkdir(parents=True, exist_ok=True)

        env = base_env.copy()
        if instance.username and (instance.password or instance.api_token):
            env["SINGULARITY_DOCKER_USERNAME"] = instance.username
            env["SINGULARITY_DOCKER_PASSWORD"] = instance.password or instance.api_token or ""

        for project in instance.projects:
            project_dir = instance_dir / _sanitize_for_fs(project.name, "project")
            project_dir.mkdir(parents=True, exist_ok=True)
            for repo in project.repositories:
                repo_leaf = _strip_project_prefix(repo.name, project.name)
                if not repo.artifacts:
                    print(f"Skipping pull for {repo.name}: no artifacts discovered.")
                    continue

                seen_refs: Set[str] = set()
                for artifact in repo.artifacts:
                    candidates: List[Tuple[str, str]] = []
                    if artifact.tags:
                        candidates.extend((f"{repo.name}:{tag}", tag) for tag in artifact.tags if tag)
                    if not candidates and artifact.digest:
                        candidates.append((f"{repo.name}@{artifact.digest}", artifact.digest))

                    for reference, label in candidates:
                        if reference in seen_refs:
                            continue
                        seen_refs.add(reference)

                        filename_label = _sanitize_for_fs(label or "image", "image")
                        outfile = project_dir / f"{_sanitize_for_fs(repo_leaf or repo.name, 'repository')}-{filename_label}.sif"
                        if outfile.exists() and not overwrite:
                            print(f"Skipping existing image {outfile}")
                            continue

                        primary_transport = transport
                        fallback_transport = "docker" if primary_transport == "oras" else "oras"
                        attempted_fallback = False

                        def run_pull(selected_transport: str) -> bool:
                            uri = f"{selected_transport}://{registry_host}/{reference}"
                            cmd = [binary_path, "pull", "--disable-cache"]
                            if overwrite:
                                cmd.append("--force")
                            cmd.extend([str(outfile), uri])
                            result = subprocess.run(cmd, capture_output=True, text=True, env=env)
                            if result.returncode != 0:
                                message = result.stderr.strip() or result.stdout.strip()
                                print(f"Failed to pull {uri}: {message}", file=sys.stderr)
                                return False
                            print(f"Pulled {uri} -> {outfile}")
                            return True

                        success = run_pull(primary_transport)
                        if (
                            not success
                            and fallback_opposite
                            and fallback_transport != primary_transport
                        ):
                            attempted_fallback = True
                            success = run_pull(fallback_transport)
                        if not success and attempted_fallback:
                            print(
                                f"Fallback to {fallback_transport} also failed for {reference} from {registry_host}.",
                                file=sys.stderr,
                            )


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
    instances = _prepare_instances(args)
    global_filters, global_lookup = _prepare_project_filters(getattr(args, "projects", None))

    sections: List[str] = []

    for instance in instances:
        instance_filters, instance_lookup = _prepare_project_filters(instance.projects)
        effective_filters = instance_filters if instance_filters is not None else global_filters
        effective_lookup = instance_lookup if instance_lookup else global_lookup
        remaining_filters = set(effective_filters) if effective_filters else set()

        ensure_instance_credentials(instance, args)
        session = build_session(instance, insecure=args.insecure)

        projects: List[Tuple[str, int]] = []
        for project in fetch_paginated(
            session,
            instance.base_url,
            "/api/v2.0/projects",
            page_size=args.page_size,
            timeout=args.timeout,
        ):
            name = str(project.get("name", ""))
            if not name:
                continue
            normalized_name = name.lower()
            if effective_filters and normalized_name not in effective_filters:
                continue
            remaining_filters.discard(normalized_name)
            repo_count = int(project.get("repo_count", 0) or 0)
            projects.append((name, repo_count))

        header = f"Harbor: {instance.base_url}"
        if not projects:
            sections.append(f"{header}\nNo projects found.")
        else:
            projects.sort(key=lambda item: item[0].lower())
            project_lines = "\n".join(f"{name} ({count} repositories)" for name, count in projects)
            sections.append(f"{header}\n{project_lines}")

        if remaining_filters:
            missing = ", ".join(sorted(effective_lookup[key] for key in remaining_filters))
            print(f"Warning: requested projects not found in {instance.base_url}: {missing}", file=sys.stderr)

    output_text = "\n\n".join(sections) if sections else "No projects found."

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
    if suffix in {".xml", ".confluence"}:
        return "confluence"
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
        if output_format == "markdown":
            args.output = DEFAULT_MARKDOWN_OUTPUT_FILENAME
        elif output_format == "confluence":
            args.output = DEFAULT_CONFLUENCE_OUTPUT_FILENAME
        else:
            args.output = DEFAULT_HTML_OUTPUT_FILENAME
    columns = _prepare_columns(getattr(args, "columns", None))
    try:
        instances = collect_data(args)
    except requests.HTTPError as exc:
        response = exc.response
        details = f"[{response.status_code}] {response.text}" if response is not None else str(exc)
        raise SystemExit(f"Harbor API error: {details}")
    except requests.RequestException as exc:
        raise SystemExit(f"Network error while contacting Harbor: {exc}") from exc

    if getattr(args, "pull_dir", None):
        pull_singularity_images(
            instances,
            pull_dir=args.pull_dir,
            transport=args.pull_transport,
            singularity_bin=args.singularity_bin,
            overwrite=getattr(args, "pull_overwrite", False),
            fallback_opposite=getattr(args, "pull_fallback", False),
        )

    if output_format == "markdown":
        summary = build_markdown(instances, columns)
        label = "Markdown"
    elif output_format == "confluence":
        summary = build_confluence_storage(instances, columns)
        label = "Confluence storage"
    else:
        summary = build_html(instances, columns)
        label = "HTML"
    output_path = Path(args.output)
    output_path.write_text(summary, encoding="utf-8")
    print(f"Wrote {label} summary to {output_path.resolve()}")


if __name__ == "__main__":
    main()
