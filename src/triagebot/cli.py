"""TriageBot CLI entry point."""

from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> None:
    """Run TriageBot from the command line."""
    parser = argparse.ArgumentParser(
        prog="triagebot",
        description="Kubernetes-first production troubleshooting agent",
    )
    subparsers = parser.add_subparsers(dest="command")

    # triagebot investigate "checkout service is slow"
    investigate = subparsers.add_parser("investigate", help="Start a triage investigation")
    investigate.add_argument("symptom", help="Description of the symptom to investigate")
    investigate.add_argument("--namespace", default="default", help="Kubernetes namespace")
    investigate.add_argument("--dry-run", action="store_true", help="Preview without remediation")
    investigate.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    # triagebot serve
    serve = subparsers.add_parser("serve", help="Start the API server")
    serve.add_argument("--host", default="0.0.0.0", help="Bind host")
    serve.add_argument("--port", type=int, default=8000, help="Bind port")

    # triagebot version
    subparsers.add_parser("version", help="Show version")

    args = parser.parse_args(argv)

    if args.command == "version":
        from triagebot import __version__

        print(f"triagebot {__version__}")
    elif args.command == "serve":
        import uvicorn

        uvicorn.run("triagebot.api.server:app", host=args.host, port=args.port, workers=1)
    elif args.command == "investigate":
        print(f"[TRIAGE] Investigating: {args.symptom}")
        print(f"[TRIAGE] Namespace: {args.namespace}")
        print("[TRIAGE] Agent framework: Strands Agents SDK")
        print("[INFO] Investigation engine not yet implemented - see task_phase2_orchestrator")
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
