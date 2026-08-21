from __future__ import annotations

from collections.abc import Sequence
import logging
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .config import get_config
from .exceptions import BrightDataInfraError, ScrapeFailed, format_scrape_signature
from .models.results import ScrapeOutcome
from .registry import get_scrapers
from .storage import EscalationStore, RunStore, ScrapeDB

logger = logging.getLogger(__name__)

_HOSTS_YAML = Path(__file__).parent / "hosts.yaml"


def _load_host_map() -> dict[str, str]:
    """Load host -> site mapping from hosts.yaml (lower-cased hosts)."""
    with open(_HOSTS_YAML, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {str(host).lower(): str(site) for host, site in raw.items()}


HOST_TO_SITE: dict[str, str] = _load_host_map()


def reload_host_map() -> None:
    """Reload the host map from disk (useful for tests or hot-reload)."""
    global HOST_TO_SITE
    HOST_TO_SITE = _load_host_map()


def resolve_site(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    site = HOST_TO_SITE.get(host)
    if site is None:
        raise ValueError(
            f"Unknown host: {host} (url={url}). "
            f"Add it to src/scraping/hosts.yaml"
        )
    return site


async def scrape(url: str) -> ScrapeOutcome:
    """Two-hop dispatch + scraper-level fallback (spec §4.1, §5.13).

    hop1: host -> site
    hop2: site -> ordered scraper list
    Tries each scraper in order; ScrapeFailed triggers fallback to next.
    BrightDataInfraError bypasses fallback (D21) — immediate alert + escalation.
    On list exhaustion → EscalationStore.upsert (spec §5.12).
    """
    site = resolve_site(url)
    scraper_classes = get_scrapers(site)
    if not scraper_classes:
        raise ValueError(f"No scrapers registered for site: {site}")

    failures: list[ScrapeFailed] = []

    for scraper_cls in scraper_classes:
        scraper = scraper_cls()
        try:
            result = await scraper.scrape(url)
            return result
        except BrightDataInfraError as e:
            logger.error("INFRA ALERT (site=%s url=%s): %s", site, url, e)
            _write_escalation(
                signature=f"{site}|infra_failure|",
                reason="infra_failure",
                snapshot={
                    "site": site,
                    "url": url,
                    "scraper": scraper_cls.__name__,
                    "status_code": e.status_code,
                    "message": str(e),
                },
            )
            raise
        except ScrapeFailed as e:
            logger.warning(
                "Scraper %s failed for %s at stage=%s, trying next",
                scraper_cls.__name__, url, e.failed_stage,
            )
            failures.append(e)

    # All scrapers exhausted
    last = failures[-1] if failures else None
    reason = _derive_reason(failures)
    signature = _derive_signature(site, failures)
    _write_escalation(
        signature=signature,
        reason=reason,
        snapshot={
            "site": site,
            "url": url,
            "attempts": [
                {
                    "scraper": f.scraper_name,
                    "failed_stage": f.failed_stage,
                    "errors": f.errors,
                }
                for f in failures
            ],
            "snapshot_preview": (last.snapshot[:2000] if last and last.snapshot else None),
        },
        run_ids=[failure.run_id for failure in failures if failure.run_id is not None],
    )
    raise ScrapeFailed(
        site=site,
        url=url,
        scraper_name="all_exhausted",
        failed_stage="scraper_fallback_exhausted",
        signature=(site, reason, ""),
        errors=[str(f) for f in failures],
        snapshot=last.snapshot if last else None,
        run_id=last.run_id if last else None,
    )


def _derive_reason(failures: list[ScrapeFailed]) -> str:
    """Map the terminal failure stages to an EscalationReason."""
    if not failures:
        return "parser_broken"
    # If ALL failures were extraction-side infra errors (Web Unlocker /
    # Datasets / DCA channels each independently returning empty bodies or
    # BD error headers), this is a genuine multi-channel infra failure —
    # preserve that signal in the escalation reason.
    if failures and all(
        f.signature and f.signature[1] in ("extraction_infra", "api_infra")
        for f in failures
    ):
        return "infra_failure"
    last_stage = failures[-1].failed_stage
    if last_stage == "api_malformed":
        return "api_malformed"
    if last_stage == "api_fetch":
        return "api_malformed"
    return "parser_broken"


def _derive_signature(site: str, failures: list[ScrapeFailed]) -> str:
    """Compose the signature `{site}|{field_or_rule}|{parser_version}`."""
    if not failures:
        return format_scrape_signature(site)
    last = failures[-1]
    return format_scrape_signature(site, last.signature, last.failed_stage)


def _write_escalation(
    signature: str,
    reason: str,
    snapshot: dict,
    run_ids: Sequence[int] = (),
) -> None:
    """Best-effort escalation write."""
    db: ScrapeDB | None = None
    try:
        cfg = get_config()
        db = ScrapeDB(cfg.db_path)
        db.init_db()
        escalation_id = EscalationStore(db).upsert(
            signature=signature,
            reason=reason,
            snapshot=snapshot,
        )
        RunStore(db).attach_escalation(run_ids, escalation_id)
        logger.info("escalation written: signature=%s reason=%s", signature, reason)
    except Exception:
        logger.exception("failed to write escalation (non-fatal)")
    finally:
        if db is not None:
            db.close()
