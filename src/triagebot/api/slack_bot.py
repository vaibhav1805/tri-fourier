"""Slack bot integration for TriageBot.

Handles /triage slash command and interactive approval buttons.
"""

from __future__ import annotations

from typing import Any

import structlog
from slack_bolt.async_app import AsyncApp
from slack_sdk.web.async_client import AsyncWebClient

from triagebot.agents.orchestrator import get_engine
from triagebot.config.settings import get_settings
from triagebot.models.findings import ConfidenceLevel, InvestigationResult

logger = structlog.get_logger()


def create_slack_app() -> AsyncApp | None:
    """Create and configure the Slack Bolt app.

    Returns None if Slack credentials are not configured.
    """
    settings = get_settings()
    if not settings.slack_bot_token:
        logger.info("slack.disabled", reason="No SLACK_BOT_TOKEN configured")
        return None

    slack_app = AsyncApp(
        token=settings.slack_bot_token,
        signing_secret=settings.slack_signing_secret,
    )

    @slack_app.command("/triage")
    async def handle_triage_command(ack: Any, body: dict[str, Any], client: AsyncWebClient) -> None:
        """Handle /triage slash command."""
        await ack()

        symptom = body.get("text", "").strip()
        channel = body.get("channel_id", "")
        user = body.get("user_id", "")

        if not symptom:
            await client.chat_postMessage(
                channel=channel,
                text="Usage: `/triage <symptom description>`\n"
                "Example: `/triage checkout service is slow`",
            )
            return

        # Post initial message
        msg = await client.chat_postMessage(
            channel=channel,
            text=f":mag: *Starting investigation...*\n"
            f"> {symptom}\n"
            f"Requested by <@{user}>",
        )
        thread_ts = msg["ts"]

        # Run investigation
        await client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=":hourglass: Querying service dependencies and dispatching specialist agents...",
        )

        engine = get_engine()
        result = await engine.investigate(symptom=symptom)

        # Post results
        await _post_investigation_result(client, channel, thread_ts, result)

    @slack_app.action("approve_remediation")
    async def handle_approve(ack: Any, body: dict[str, Any], client: AsyncWebClient) -> None:
        """Handle remediation approval button click."""
        await ack()
        user = body.get("user", {}).get("id", "unknown")
        channel = body.get("channel", {}).get("id", "")

        await client.chat_postMessage(
            channel=channel,
            text=f":white_check_mark: Remediation approved by <@{user}>. Executing...",
        )
        # TODO: Wire to actual remediation execution

    @slack_app.action("deny_remediation")
    async def handle_deny(ack: Any, body: dict[str, Any], client: AsyncWebClient) -> None:
        """Handle remediation denial button click."""
        await ack()
        user = body.get("user", {}).get("id", "unknown")
        channel = body.get("channel", {}).get("id", "")

        await client.chat_postMessage(
            channel=channel,
            text=f":x: Remediation denied by <@{user}>. Escalating to on-call.",
        )

    return slack_app


async def _post_investigation_result(
    client: AsyncWebClient,
    channel: str,
    thread_ts: str,
    result: InvestigationResult,
) -> None:
    """Post formatted investigation results to a Slack thread."""
    findings_text = ""
    for i, f in enumerate(result.findings, 1):
        evidence_str = "\n".join(f"  - {e}" for e in f.evidence[:3])
        findings_text += (
            f"\n*Finding {i}:* [{f.severity.upper()}] (confidence: {f.confidence:.0%})\n"
            f"{f.summary}\n"
            f"Evidence:\n{evidence_str}\n"
        )

    confidence_emoji = {
        ConfidenceLevel.AUTO_REMEDIATE: ":rocket:",
        ConfidenceLevel.APPROVAL_REQUIRED: ":warning:",
        ConfidenceLevel.HUMAN_APPROVAL: ":raised_hand:",
        ConfidenceLevel.REPORT_ONLY: ":clipboard:",
    }.get(result.confidence_level, ":question:")

    summary = (
        f"{confidence_emoji} *Investigation Complete*\n\n"
        f"*Root Cause:* {result.root_cause or 'Unknown'}\n"
        f"*Confidence:* {result.aggregate_confidence:.0%} ({result.confidence_level.value})\n"
        f"*Affected Services:* {', '.join(result.affected_services) or 'None identified'}\n"
        f"\n---\n{findings_text}"
    )

    blocks: list[dict[str, Any]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": summary}},
    ]

    # Add approval buttons if needed
    if result.confidence_level in (
        ConfidenceLevel.APPROVAL_REQUIRED,
        ConfidenceLevel.HUMAN_APPROVAL,
    ):
        remediation_text = "No remediation suggested"
        if result.findings:
            best = max(result.findings, key=lambda f: f.confidence)
            if best.suggested_remediation:
                remediation_text = best.suggested_remediation

        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Suggested Remediation:* {remediation_text}",
            },
        })
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve Remediation"},
                    "style": "primary",
                    "action_id": "approve_remediation",
                    "value": result.investigation_id,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Deny"},
                    "style": "danger",
                    "action_id": "deny_remediation",
                    "value": result.investigation_id,
                },
            ],
        })

    await client.chat_postMessage(
        channel=channel,
        thread_ts=thread_ts,
        text=f"Investigation complete: {result.root_cause or 'Unknown root cause'}",
        blocks=blocks,
    )
