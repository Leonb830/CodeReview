"""The same evidence and coverage rendered for humans locally and on GitHub."""

import html
import re

from codereview.models import Finding, FixResult, ReviewResult


def plain(text: str) -> str:
    """Prevent model/source strings from injecting HTML, mentions, or Markdown controls."""
    text = html.escape(text).replace("@", "@\u200b")
    return re.sub(r"([\\`*_[\]{}()#+!|>~])", r"\\\1", text)


def render_finding(finding: Finding) -> str:
    lines = [
        f"### {finding.id}: {plain(finding.title)}",
        "",
        f"**{finding.kind} · {finding.severity} · {finding.category} · {finding.verification}**",
        "",
        plain(finding.observation),
        "",
        f"**Why it matters:** {plain(finding.rationale)}",
        "",
        f"**Suggested change:** {plain(finding.alternative)}",
    ]
    for evidence in finding.evidence:
        lines += [
            "",
            f"**Source:** {plain(evidence.path)}:{evidence.start_line}–{evidence.end_line}",
            "",
            "<pre>" + html.escape(evidence.quote).replace("@", "@\u200b") + "</pre>",
        ]
    if finding.uncertainty:
        lines += ["", f"**Uncertainty:** {plain(finding.uncertainty)}"]
    if finding.origin != "unknown":
        lines += ["", f"**Origin:** {finding.origin}"]
    return "\n".join(lines)


def render_review(result: ReviewResult) -> str:
    lines = [
        f"## Code review · {result.status}",
        "",
        f"Run `{result.run_id}` · revision `{result.snapshot.head_sha[:12]}` · snapshot `{result.snapshot.id[:12]}`",
        "",
        plain(result.summary)
        or f"{len(result.findings)} findings across {len(result.completed_tasks)} completed tasks.",
        "",
        f"Model usage: workers **${result.usage.worker_cost:.4f}**, coordinator **${result.usage.coordinator_cost:.4f}**.",
    ]
    if result.usage.worker_reserved or result.usage.coordinator_reserved:
        lines += [
            "",
            f"Unsettled reservations: workers ${result.usage.worker_reserved:.4f}; coordinator ${result.usage.coordinator_reserved:.4f}. These may still be billable.",
        ]
    if result.gaps:
        lines += ["", "### Incomplete coverage", ""]
        lines.extend(f"- {plain(gap)}" for gap in result.gaps)
    if result.snapshot.excluded:
        lines += ["", "<details><summary>Excluded paths and scope limits</summary>", ""]
        lines.extend(f"- {plain(item)}" for item in result.snapshot.excluded)
        lines += ["", "</details>"]
    if not result.findings:
        lines += [
            "",
            "No supported findings were produced. "
            + (
                "Coverage is incomplete; this is not a clean review."
                if result.status != "complete"
                else "This is not proof that the code is defect-free."
            ),
        ]
    for kind in ("defect", "suggestion"):
        subset = [finding for finding in result.findings if finding.kind == kind]
        if subset:
            lines += [
                "",
                "## " + ("Defects" if kind == "defect" else "Improvement suggestions"),
                "",
            ]
            lines += [render_finding(finding) + "\n" for finding in subset]
    if result.findings:
        lines += [
            "",
            "Request selected fixes with `/review fix F...`, or explicitly request `/review fix-all`. Findings apply only to this reviewed revision.",
        ]
    return "\n".join(lines) + "\n"


def render_fix(result: FixResult) -> str:
    lines = [
        f"## Requested fix · {result.status}",
        "",
        f"Review `{result.review_run_id}` · selected findings: {', '.join(result.selected_ids)}",
    ]
    if result.commit_sha:
        lines += ["", f"Commit `{result.commit_sha}` on `{plain(result.branch or '')}`."]
    lines += ["", "### Validation", "", "| Check | Baseline | Fixed |", "|---|---|---|"]
    baseline = {check.name: check.status for check in result.baseline_checks}
    for check in result.checks:
        lines.append(
            f"| {plain(check.name)} | {baseline.get(check.name, 'not run')} | {check.status} |"
        )
    if not result.checks:
        lines.append("| No checks completed | — | unavailable |")
    for stage, checks in (("Baseline", result.baseline_checks), ("Fixed", result.checks)):
        for check in checks:
            if check.status != "passed" and check.output:
                diagnostic = check.output[:4000]
                if len(check.output) > 4000:
                    diagnostic += (
                        "\n[report excerpt truncated; full captured output is in the run record]"
                    )
                lines += [
                    "",
                    f"<details><summary>{stage}: {html.escape(check.name)} ({check.status})</summary>",
                    "",
                    "<pre>" + html.escape(diagnostic).replace("@", "@\u200b") + "</pre>",
                    "",
                    "</details>",
                ]
    if result.notes:
        lines += ["", *[f"- {plain(note)}" for note in result.notes]]
    lines += [
        "",
        f"Model usage: workers ${result.usage.worker_cost:.4f}; coordinator ${result.usage.coordinator_cost:.4f}.",
        "",
        "A validated result describes the checks recorded above. Merge remains a human decision.",
    ]
    return "\n".join(lines) + "\n"
