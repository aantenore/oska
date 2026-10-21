"""`oska pack check` and `oska pack build`: checking and building one pack.

Not registered on the main application; see the kit lane's report for the
`cli/main.py` line that attaches `app` under the name `pack`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer

from oska.cli.options import JsonOption, fail
from oska.kit.build import build_pack
from oska.kit.check import PackReport, check_pack

__all__ = ["app"]

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Check or build one pack in isolation.",
)

PathArgument = Annotated[Path, typer.Argument(help="Directory of the pack, or its pack.yaml.")]
IntoOption = Annotated[
    Path | None,
    typer.Option("--into", help="Directory to write the built artifact into."),
]
AllowShellOption = Annotated[
    bool,
    typer.Option(
        "--allow-shell",
        help=(
            "Run the scenarios of a pack that binds an integration to a shell command or "
            "to a tool server started over stdio. A check runs a pack's bindings on this "
            "machine, so such a pack is refused without this flag; give it only for a pack "
            "you trust."
        ),
    ),
]


@app.command("check")
def check(
    path: PathArgument, as_json: JsonOption = False, allow_shell: AllowShellOption = False
) -> None:
    """Validate a pack, run its evaluation sets and its scenarios, and report its level.

    The scenarios run the way a platform serves the pack, as the one pack of a
    domain. A binding that runs a command is refused unless --allow-shell is given.
    """
    report = check_pack(path, allow_shell=allow_shell)
    typer.echo(json.dumps(_as_json(report), indent=2) if as_json else _as_text(report))
    if not report.ok:
        raise typer.Exit(code=1)


@app.command("build")
def build(path: PathArgument, into: IntoOption = None) -> None:
    """Write a pack as a content-addressed tarball, with a manifest listing its files."""
    destination = into or Path("dist")
    try:
        artifact = build_pack(path, destination)
    except ValueError as exc:
        fail(str(exc))
    typer.echo(f"{artifact.archive_path} ({len(artifact.files)} file(s), {artifact.content_hash})")


def _as_text(report: PackReport) -> str:
    if report.manifest is None:
        return "\n".join(["not a usable pack:", *report.errors])
    lines = [
        f"{report.manifest.name} {report.manifest.version}: "
        f"claims level {report.claimed_level}, reaches level {report.attained_level}"
    ]
    errors = [issue for issue in report.validation_issues if issue.severity == "error"]
    warnings = [issue for issue in report.validation_issues if issue.severity == "warning"]
    lines.extend(f"error: {issue}" for issue in errors)
    lines.extend(f"warning: {issue}" for issue in warnings)
    lines.extend(f"error: {message}" for message in report.errors)
    if report.evaluation is not None:
        precision = report.evaluation.precision
        lines.append(f"evaluation: {report.evaluation.cases} case(s), precision {precision}")
        lines.extend(
            f"  failed: {result.skill_id} expects {result.expects!r} of {result.text!r}"
            for result in report.evaluation.failures
        )
    if report.scenarios is not None:
        for level, (passed, total) in sorted(report.scenarios.by_level.items()):
            lines.append(f"scenarios level {level}: {passed}/{total} pass")
        for result in report.scenarios.results:
            for failure in result.failures:
                lines.append(f"  failed: {result.name}: {failure}")
    lines.append("ok" if report.ok else "not conformant")
    return "\n".join(lines)


def _as_json(report: PackReport) -> dict:
    return report.model_dump(mode="json")
