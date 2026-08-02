"""Command line entry point for the bookmark retriever.

    python cli.py auth                 one-time consent, stores a refresh token
    python cli.py fetch                pull bookmarks, no content resolution
    python cli.py resolve              resolve links already in the store
    python cli.py run                  fetch then resolve
    python cli.py status               what is on disk right now

Every command is safe to re-run. The store is append-only by design.
"""

import argparse
import collections
import logging
import sys
from pathlib import Path

from x_bookmarks import auth, fetch, resolve, store

###############################################################################
# Setup
###############################################################################


def configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="x-bookmarks",
        description="Retrieve X bookmarks and the content behind them.",
    )
    parser.add_argument("--store", type=Path, default=store.DEFAULT_STORE,
                        help="store directory (default: ./store)")
    parser.add_argument("--verbose", action="store_true")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("auth", help="run the one-time consent flow")

    fetch_parser = subparsers.add_parser("fetch", help="retrieve bookmarks")
    fetch_parser.add_argument("--max-pages", type=int, default=None,
                              help="stop after N pages (100 bookmarks each)")
    fetch_parser.add_argument("--full", action="store_true",
                              help="walk every page instead of stopping at "
                                   "the first page with nothing new")

    resolve_parser = subparsers.add_parser(
        "resolve", help="resolve links for records already stored")
    resolve_parser.add_argument("--all", action="store_true",
                                help="re-resolve links already marked verified")
    resolve_parser.add_argument("--limit", type=int, default=None,
                                help="resolve at most N records this run")
    resolve_parser.add_argument(
        "--repair", action="store_true",
        help="reset X-post links resolved by the old scraping path, then "
             "re-resolve them through the API")

    run_parser = subparsers.add_parser("run", help="fetch then resolve")
    run_parser.add_argument("--max-pages", type=int, default=None)
    run_parser.add_argument("--all", action="store_true")
    run_parser.add_argument("--full", action="store_true",
                            help="walk every page rather than stopping early")

    subparsers.add_parser("status", help="summarize the store")
    return parser


###############################################################################
# Commands
###############################################################################


def command_auth() -> int:
    """One-time consent. Prints the URL, takes the pasted redirect back."""
    app = auth.load_app_config()
    url, handler = auth.build_authorization_url(app)

    print("\n1. Open this URL and authorize the app:\n")
    print(f"   {url}\n")
    print("2. Your browser will be redirected to a URL that fails to load.")
    print("   That failure is expected -- nothing is listening on that port.")
    print("   Copy the FULL address bar contents and paste it below.\n")

    redirect_response = input("Redirect URL: ").strip()
    if not redirect_response:
        print("Nothing pasted. Consent not completed.", file=sys.stderr)
        return 1

    auth.complete_consent(handler, redirect_response)
    print(f"\nToken stored at {auth.TOKEN_PATH}. Consent will not be needed again.")
    return 0


def command_fetch(root: Path, max_pages: int | None, full: bool) -> fetch.FetchResult:
    """Retrieve bookmarks, persisting every page as soon as it arrives.

    Incremental by default: stop once a page holds nothing new. `--full`
    walks every page, which is what a first pull and any repair needs.
    Per-page persistence is what makes the interrupt handler's promise true:
    a Ctrl-C mid-pagination keeps every page already retrieved.
    """
    token = auth.get_access_token()
    known = None if full else store.known_ids(root)
    if known:
        print(f"incremental -- {len(known)} bookmarks already stored")

    totals = {"added": 0, "updated": 0, "total": 0}

    def persist_page(page_records: list) -> None:
        counts = store.save_records(root, page_records)
        for key in totals:
            totals[key] += counts[key]

    result = fetch.fetch_bookmarks(token, max_pages=max_pages,
                                   known_ids=known, on_page=persist_page)
    if not result["records"]:
        print("No bookmarks returned.")
        return result

    print(f"fetched {totals['total']}: {totals['added']} new, "
          f"{totals['updated']} already stored")
    if not result["complete"]:
        # An incremental re-run will early-stop before reaching what was
        # missed; only --full can recover it before it ages out of the window.
        print("fetch INCOMPLETE -- a page failed or the page cap stopped "
              "early. Run 'python cli.py fetch --full' to recover the gap.",
              file=sys.stderr)
    return result


def repair_scraped_x_posts(root: Path) -> int:
    """Reset X-post links that an earlier build resolved by scraping.

    Those were stored `verified` while holding a few hundred characters of
    reply metadata rather than the post. Because the merge logic protects
    verified content, they cannot be corrected by a normal re-run -- they have
    to be knocked back to pending explicitly. One-time, opt-in via --repair.
    """
    repaired = 0
    for record in list(store.iter_records(root)):
        links = record.get("links") or []
        touched = False
        for link in links:
            if link.get("kind") == "x_post" and link.get("status") == "verified":
                link.update(status="pending", text="", reason="")
                touched = True
        if touched:
            record["resolve_status"] = "pending"
            store.write_json_atomic(
                store.record_path(root, record["id"]), record)
            repaired += 1
    return repaired


def command_resolve(root: Path, resolve_all: bool, limit: int | None) -> int:
    """Resolve links for stored records, persisting each as it completes."""
    pending = [
        record for record in store.iter_records(root)
        if resolve_all or record.get("resolve_status") in (None, "", "pending")
    ]
    if limit:
        pending = pending[:limit]

    if not pending:
        print("Nothing to resolve. Use --all to re-resolve verified links.")
        return 0

    # X posts come from the API, never from scraping. Most are already in hand
    # as quoted-post text captured at fetch time; only the remainder cost a
    # lookup, batched 100 at a time.
    all_records = list(store.iter_records(root))
    post_lookup = resolve.post_lookup_from_records(all_records)
    seeded = len(post_lookup)

    missing = resolve.missing_x_post_ids(pending, post_lookup,
                                         skip_resolved=not resolve_all)
    if missing:
        client = fetch.build_client(auth.get_access_token())
        post_lookup.update(fetch.fetch_posts(client, missing))

    # A resolve whose write failed did not happen. Count persistence failures
    # here so the run cannot report success while content quietly went unsaved.
    persist_failures: list[str] = []

    def persist(record: dict) -> None:
        try:
            store.put_record(root, record)
        except Exception as exc:
            persist_failures.append(str(record.get("id")))
            print(f"FAILED to persist {record.get('id')}: {exc}", file=sys.stderr)

    print(f"resolving {len(pending)} records "
          f"({seeded} X posts seeded from quotes, {len(missing)} looked up)")
    resolve.resolve_records(
        pending,
        skip_resolved=not resolve_all,
        on_resolved=persist,
        post_lookup=post_lookup,
    )
    if persist_failures:
        print(f"resolved {len(pending)} records but {len(persist_failures)} "
              "FAILED to persist -- re-run resolve to retry them",
              file=sys.stderr)
        return 1
    print(f"resolved {len(pending)} records")
    return 0


def command_status(root: Path) -> None:
    """What is actually on disk, by honest status."""
    records = list(store.iter_records(root))
    if not records:
        print(f"Store at {root.resolve()} is empty.")
        return

    by_status = collections.Counter(
        record.get("resolve_status") or "pending" for record in records
    )
    link_statuses = collections.Counter(
        link.get("status") or "pending"
        for record in records
        for link in (record.get("links") or [])
    )
    with_text = sum(
        1 for record in records
        for link in (record.get("links") or [])
        if link.get("text")
    )

    print(f"store:     {root.resolve()}")
    print(f"bookmarks: {len(records)}")
    print("\nrecords by status:")
    for status, count in by_status.most_common():
        print(f"  {status:<12} {count}")
    print("\nlinks by status:")
    for status, count in link_statuses.most_common():
        print(f"  {status:<12} {count}")
    print(f"\nlinks with stored content: {with_text}")


def dispatch(args: argparse.Namespace) -> int:
    root: Path = args.store

    if args.command == "auth":
        return command_auth()

    if args.command == "fetch":
        result = command_fetch(root, args.max_pages, args.full)
        return 0 if result["complete"] else 1

    if args.command == "resolve":
        if args.repair:
            count = repair_scraped_x_posts(root)
            print(f"reset {count} records holding scraped X-post text")
        return command_resolve(root, args.all, args.limit)

    if args.command == "run":
        result = command_fetch(root, args.max_pages, args.full)
        resolve_code = 0
        if result["records"]:
            resolve_code = command_resolve(root, args.all, None)
        return resolve_code if result["complete"] else 1

    if args.command == "status":
        command_status(root)
        return 0

    return 1


###############################################################################
# Execution
###############################################################################

parser = build_parser()
parsed_args = parser.parse_args()
configure_logging(parsed_args.verbose)

try:
    exit_code = dispatch(parsed_args)
except KeyboardInterrupt:
    print("\ninterrupted -- everything retrieved so far is already on disk")
    exit_code = 130
except RuntimeError as error:
    print(f"\n{error}", file=sys.stderr)
    exit_code = 1

sys.exit(exit_code)
