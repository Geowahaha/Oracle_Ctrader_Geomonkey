"""Standalone CLI for one Governor tick — designed for cron / systemd timers.

Examples:
    # Quiet cron run, exits non-zero on misconfiguration only:
    python -m learning.self_mutation.run_tick

    # Verbose run for ad-hoc inspection (prints the TickReport):
    python -m learning.self_mutation.run_tick --verbose

The script honours `SELF_MUTATION_ENABLED`. When disabled, it logs a one-line
notice and exits 0 so cron jobs don't repeatedly alarm.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict

from learning.self_mutation.governor import Governor
from learning.self_mutation.notify import LoggingNotifier
from learning.self_mutation.telegram_notifier import build_default_telegram_notifier


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run a single Self-Mutation Governor tick.")
    p.add_argument("--verbose", "-v", action="store_true", help="Print the TickReport as JSON.")
    p.add_argument("--no-telegram", action="store_true", help="Force LoggingNotifier even if Telegram is wired.")
    return p.parse_args(argv)


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    _setup_logging(args.verbose)
    log = logging.getLogger("self_mutation.run_tick")

    from config import config

    notifier = LoggingNotifier()
    if not args.no_telegram and bool(getattr(config, "SELF_MUTATION_NOTIFY_TELEGRAM", True)):
        telegram = build_default_telegram_notifier(enabled=True)
        if telegram is not None:
            notifier = telegram
        else:
            log.info("self_mutation_telegram_unavailable_falling_back_to_logging")

    governor = Governor.from_config(config_obj=config, notifier=notifier)
    report = governor.tick()
    if args.verbose:
        payload = asdict(report)
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    log.info(
        "self_mutation_tick_done enabled=%s losses=%d mutations=%d verdicts=%d passed=%d "
        "canaries_promoted=%d mains_promoted=%d canaries_reverted=%d mains_rolled_back=%d",
        governor.enabled,
        report.new_loss_events,
        report.mutations_proposed,
        report.verdicts_recorded,
        report.verdicts_passed,
        report.canaries_promoted,
        report.mains_promoted,
        report.canaries_reverted,
        report.mains_rolled_back,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
