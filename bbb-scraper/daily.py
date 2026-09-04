#!/usr/bin/env python3
"""The 9am run: pick today's lists, pull them, enrich them, report.

Designed around the fact that nobody is watching. An unattended job that dies
quietly looks exactly like an unattended job with nothing to report, so this
one is loud about failure: it always writes a status file, and on failure it
drops an ATTENTION-<date>.txt into the same folder the sheets land in -- the
folder that gets opened every morning anyway.

Stages per list:
    scrape BBB -> Apollo website lookup + website filter -> owner/email
    -> headcount gate -> HubSpot dedupe -> sheet

It stops at the sheet. Nothing is pushed to outreach: a wrong match that only
costs a deleted row is a nuisance, while a wrong match that sends mail is a
stranger getting a cold email from you.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import traceback
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
STATE_FILE = ".rotation-state.json"


# --------------------------------------------------------------------------
# config + rotation state
# --------------------------------------------------------------------------

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        config = json.load(fh)
    if not config.get("metros"):
        raise ValueError(f"{path} has no metros to rotate through")
    if not config.get("schedule"):
        raise ValueError(f"{path} has no schedule")
    return config


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"metro_index": 0, "history": []}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        # Losing the cursor costs a repeated metro, not a broken run.
        return {"metro_index": 0, "history": []}


def save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2)
    os.replace(tmp, path)


def todays_lists(config: dict, state: dict, when: dt.date) -> List[dict]:
    """(category, metro) for each list due today.

    The metro advances per list, not per day, so two lists on the same morning
    cover two different cities rather than doubling up on one.
    """
    categories = config["schedule"].get(DAYS[when.weekday()], [])
    metros = config["metros"]
    plan = []
    index = state.get("metro_index", 0)
    for offset, category in enumerate(categories):
        plan.append({"category": category,
                     "metro": metros[(index + offset) % len(metros)]})
    return plan


# --------------------------------------------------------------------------
# one list
# --------------------------------------------------------------------------

def scrape(config: dict, category: str, metro: str, out_path: str,
           extra: Optional[List[str]] = None) -> int:
    import scraper

    argv = [
        "--category", category,
        "--location", metro,
        "--output", out_path,
        "--column-map", os.path.join(HERE, "lead-format.json"),
        "--max-results", str(config.get("max_results", 60)),
        "--target-rows", str(config.get("target_rows", 15)),
        "--apollo",
        "--report", out_path.replace(".csv", ".json"),
    ]
    # Deliberately NOT --require-website. Websites come from the profile page,
    # Apollo backfills the rest, and a company Apollo has never indexed would
    # therefore be dropped for having a blank website -- which is exactly the
    # company this scrape exists to find.
    #
    # Profile pages sit behind a Cloudflare challenge that neither plain HTTP,
    # headless Chromium, nor a real Chrome under automation gets through. So
    # years_in_business and BBB's own headcount are simply not available, and
    # attempting the detail pass costs seconds per listing to prove it again
    # every morning. `detail: true` in the config re-enables it if BBB ever
    # relents -- or once a clearance cookie is supplied.
    if not config.get("detail"):
        argv += ["--no-detail"]
    elif config.get("min_years"):
        # min-years reads a profile-page field; it can only screen when the
        # detail pass actually runs.
        argv += ["--min-years", str(config["min_years"])]
    if config.get("exclude_file"):
        path = config["exclude_file"]
        if not os.path.isabs(path):
            path = os.path.join(HERE, path)
        argv += ["--exclude-file", path]
    argv += extra or []
    return scraper.main(argv)


def enrich_contacts(config: dict, csv_path: str) -> dict:
    """Owner name + work email + the headcount gate, in place on the CSV."""
    import apollo_people
    import parse

    key = apollo_people.resolve_api_key(None)
    if not key:
        return {"skipped": "no APOLLO_API_KEY"}

    with open(csv_path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
        fieldnames = list(rows[0].keys()) if rows else []
    if not rows:
        return {"skipped": "no rows to enrich"}

    listings = []
    for row in rows:
        listings.append(parse.Listing(
            company_name=row.get("company_name", ""),
            website=row.get("website", ""),
            phone=row.get("phone", ""),
            city=row.get("city", ""),
            state=row.get("state", ""),
            apollo_org_id=row.get("apollo_org_id", ""),
        ))

    governor = apollo_people.CreditGovernor(cap=config.get("daily_credit_cap", 40))
    with apollo_people.PeopleClient(
        key,
        base_url=config.get("apollo_base", apollo_people.DEFAULT_BASE),
        governor=governor,
        cache=apollo_people.PeopleCache(
            os.path.join(HERE, ".apollo-people-cache.json")),
        min_employees=config.get("min_employees", 0),
        verbose=True,
    ) as client:
        found = apollo_people.enrich_listings(listings, client)
        stats = client.stats

    extra_columns = ["screen", "owner_first_name", "owner_last_name", "title",
                     "email", "email_status", "linkedin_url", "apollo_employees",
                     "notes"]
    for column in extra_columns:
        if column not in fieldnames:
            fieldnames.append(column)

    kept = []
    for row, listing in zip(rows, listings):
        found_row = found.get(listing.dedupe_key() or listing.company_name) or {}
        # A row the headcount gate rejected carries a "dropped:" note and no
        # contact -- leave it out of the sheet rather than shipping a blank.
        if str(found_row.get("notes", "")).startswith("dropped:"):
            continue
        for column in extra_columns:
            row.setdefault(column, "")
            if found_row.get(column) not in (None, ""):
                row[column] = found_row[column]
        row["screen"] = screen_verdict(row, config.get("min_employees") or 0)
        kept.append(row)

    # Qualified first, then the sleepers to look at, then the rest. The point
    # of the sheet is that the top of it is actionable without reading further.
    order = {"QUALIFIED": 0, "REVIEW-UNSIZED": 1, "REVIEW": 2}
    kept.sort(key=lambda r: order.get(r.get("screen", ""), 3))

    with open(csv_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(kept)

    return {
        "rows_before": len(rows),
        "rows_after": len(kept),
        "emails": stats.emails,
        "dropped_too_small": stats.too_small,
        "size_unknown": stats.size_unknown,
        "wrong_place": stats.wrong_place,
        "credit_cap_hit": stats.cap_hit,
        "credit_cap_unverified": stats.cap_unverified,
        "credits_spent": governor.spent,
        "notes": stats.notes,
    }


def read_report(csv_path: str) -> dict:
    """Apollo counters from the run report written beside the sheet."""
    path = csv_path.replace(".csv", ".json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            report = json.load(fh)
    except (OSError, ValueError):
        return {}
    apollo = report.get("apollo_stats") or report.get("apollo") or {}
    return {"apollo_errors": apollo.get("errors"),
            "apollo_matched": apollo.get("matched")}


def screen_verdict(row: dict, min_employees: int) -> str:
    """Whether this row passed the size screen, failed it, or was never sized.

    Three outcomes, not two. A company Apollo has never indexed cannot be
    sized at all -- BBB's own headcount sits behind a Cloudflare challenge --
    and calling that "passed" would put an unscreened row at the top of the
    sheet looking exactly like a screened one.
    """
    raw = str(row.get("apollo_employees", "")).strip()
    if not raw:
        return "REVIEW-UNSIZED"
    try:
        count = int(float(raw))
    except ValueError:
        return "REVIEW-UNSIZED"
    if min_employees and count < min_employees:
        return "TOO-SMALL"
    return "QUALIFIED"


def crm_dedupe(csv_path: str) -> dict:
    """Annotate the sheet in place with SEND / REVIEW / SKIP verdicts.

    Without a token this is skipped and *reported* as skipped: a sheet that
    silently never met the CRM looks identical to one that came back clean,
    and the difference is whether you email an existing customer.
    """
    if not os.environ.get("HUBSPOT_TOKEN"):
        return {"skipped": "no HUBSPOT_TOKEN -- rows were NOT checked against the CRM"}
    import crm_check

    checked = csv_path.replace(".csv", "-checked.csv")
    try:
        code = crm_check.main([csv_path, "--output", checked])
    except Exception as exc:
        return {"error": str(exc)}
    if code != 0 or not os.path.exists(checked):
        return {"error": f"crm_check exited {code}"}

    # crm_check writes a copy; fold it back so there is one sheet, not two.
    os.replace(checked, csv_path)
    with open(csv_path, encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    counts: Dict[str, int] = {}
    for row in rows:
        status = row.get("crm_verdict", "")
        counts[status] = counts.get(status, 0) + 1
    return {"counts": counts}


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def unknown_headcount(rows: List[dict]) -> Optional[float]:
    """Fraction of rows with no employee count, or None for an empty sheet."""
    if not rows:
        return None
    blank = sum(1 for row in rows if not str(row.get("employees", "")).strip())
    return blank / len(rows)


def write_status(export_dir: str, status: dict) -> None:
    path = os.path.join(export_dir, "_daily-status.json")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(status, fh, indent=2)
    except OSError:
        pass

    attention = os.path.join(export_dir,
                             f"ATTENTION-{status['date']}.txt")
    problems = status.get("problems") or []
    if not problems:
        # Clear a stale banner so yesterday's failure doesn't look like today's.
        if os.path.exists(attention):
            os.remove(attention)
        return
    try:
        with open(attention, "w", encoding="utf-8") as fh:
            fh.write(f"Lead run {status['date']} needs attention\n")
            fh.write("=" * 44 + "\n\n")
            for problem in problems:
                fh.write(f"  * {problem}\n")
            fh.write(f"\nSheets written: {len(status.get('sheets', []))}\n")
            fh.write("Full detail in _daily-status.json\n")
    except OSError:
        pass


def run(config: dict, export_dir: str, when: dt.date,
        state_path: str, dry_run: bool = False) -> dict:
    state = load_state(state_path)
    plan = todays_lists(config, state, when)
    status = {
        "date": when.isoformat(),
        "planned": plan,
        "sheets": [],
        "problems": [],
        "enrichment": [],
    }

    if not plan:
        status["problems"] = []
        status["note"] = "nothing scheduled today"
        return status

    if dry_run:
        status["note"] = "dry run -- nothing was fetched"
        return status

    for item in plan:
        stamp = when.strftime("%Y-%m-%d")
        name = f"{item['category']}-{item['metro']}-{stamp}.csv"
        out_path = os.path.join(export_dir, name)
        try:
            code = scrape(config, item["category"], item["metro"], out_path)
            if code != 0 or not os.path.exists(out_path):
                status["problems"].append(
                    f"{item['category']} in {item['metro']}: scrape failed "
                    f"(exit {code}) -- no sheet written")
                continue

            result = enrich_contacts(config, out_path)
            result["sheet"] = name
            status["enrichment"].append(result)

            apollo = read_report(out_path)
            failed = apollo.get("apollo_errors") or 0
            matched = apollo.get("apollo_matched") or 0
            if failed and not matched:
                # Every single lookup failing is a broken endpoint or a dead
                # key, not bad luck. Left in the run summary it reads as a
                # detail; it means the whole enrichment did nothing.
                status["problems"].append(
                    f"{name}: ALL {failed} Apollo lookups failed -- no websites "
                    f"recovered and no sleeper labels written. Run "
                    f"`python scraper.py --apollo-probe` to find the right "
                    f"endpoint.")

            if result.get("credit_cap_hit"):
                status["problems"].append(
                    f"{name}: stopped at the {config.get('daily_credit_cap')} "
                    f"credit daily cap -- some rows have no email")
            if result.get("credit_cap_unverified"):
                status["problems"].append(
                    f"{name}: could not read the Apollo balance, so the daily "
                    f"credit cap was NOT enforced on this run")

            crm = crm_dedupe(out_path)
            result["crm"] = crm
            if crm.get("skipped"):
                status["problems"].append(f"{name}: {crm['skipped']}")
            if crm.get("error"):
                status["problems"].append(f"{name}: CRM check failed: {crm['error']}")

            with open(out_path, encoding="utf-8") as fh:
                sheet_rows = list(csv.DictReader(fh))
            rows = len(sheet_rows)

            # The size screen reads `employees` off BBB's profile page. Those
            # pages have answered 403 before, and an unknown value PASSES the
            # filter rather than failing it -- so a blocked detail fetch turns
            # a >=20 employee screen into no screen at all, silently, and the
            # sheet still looks full. Say so.
            # With the detail pass off, BBB headcount is expected to be
            # absent; the screen runs on Apollo's headcount instead, and only
            # for companies Apollo knows. Rows it does not know are unsized by
            # design and flagged for review rather than silently kept or cut.
            gap = unknown_headcount(sheet_rows)
            if config.get("detail") and config.get("min_employees") \
                    and gap is not None and gap > 0.5:
                status["problems"].append(
                    f"{name}: headcount missing on {gap:.0%} of rows -- BBB "
                    f"profile pages are challenged, so the >= "
                    f"{config['min_employees']} employee screen did NOT run "
                    f"from BBB data.")

            unsized = sum(1 for row in sheet_rows
                          if not str(row.get("apollo_employees", "")).strip())
            if unsized:
                status.setdefault("unsized", 0)
                status["unsized"] += unsized

            status["sheets"].append({"file": name, "rows": rows,
                                     "headcount_missing": gap})
            if rows == 0:
                status["problems"].append(f"{name}: 0 rows survived filtering")

        except Exception as exc:
            status["problems"].append(
                f"{item['category']} in {item['metro']}: {exc}")
            status.setdefault("tracebacks", []).append(traceback.format_exc())

    state["metro_index"] = (state.get("metro_index", 0) + len(plan)) % len(config["metros"])
    state.setdefault("history", []).append(
        {"date": when.isoformat(), "lists": plan})
    state["history"] = state["history"][-60:]
    save_state(state_path, state)
    return status


def print_plan(config: dict, status: dict, when: dt.date, export_dir: str) -> None:
    """What this run does, and under what screen.

    A dry run that prints only "nothing was fetched" confirms nothing -- the
    reason to run one is to see the plan before spending the time.
    """
    plan = status.get("planned") or []
    print("")
    print(f"{when.strftime('%A %d %B %Y')} -- {len(plan)} list(s)")
    print("-" * 46)
    if not plan:
        print("  nothing scheduled (weekends are empty by default)")
    for item in plan:
        print(f"  {item['category']:<32} {item['metro']}")

    print("")
    print("screen")
    print("-" * 46)
    print(f"  min employees   : {config.get('min_employees') or 'none'}"
          f"   (>= $500K EBITDA proxy)")
    if config.get("detail"):
        print(f"  min years       : {config.get('min_years') or 'none'}")
    else:
        print("  min years       : NOT APPLIED -- BBB profile pages are "
              "challenged (detail: false)")
    print(f"  rows per sheet  : {config.get('target_rows')}"
          f" (from up to {config.get('max_results')} raw)")
    print(f"  apollo cap      : {config.get('daily_credit_cap')} credits/day")
    excludes = config.get("exclude_file")
    print(f"  excluding       : {excludes or 'nothing'}")
    print(f"  writing to      : {export_dir}")

    if not os.environ.get("APOLLO_API_KEY"):
        print("\n  (!) APOLLO_API_KEY not set -- no owner names or emails")
    if not os.environ.get("HUBSPOT_TOKEN"):
        print("  (!) HUBSPOT_TOKEN not set -- rows will NOT be CRM-checked")


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=os.path.join(HERE, "rotation.json"))
    p.add_argument("--export-dir", default=None,
                   help="where sheets land (default: $LEAD_EXPORT_DIR, then "
                        "%%USERPROFILE%%/ClaudeAssistant/exports)")
    p.add_argument("--date", default=None, help="YYYY-MM-DD, for testing a weekday")
    p.add_argument("--dry-run", action="store_true",
                   help="print today's plan without fetching anything")
    p.add_argument("--state", default=None, help="rotation cursor file")
    args = p.parse_args(argv)

    export_dir = (args.export_dir or os.environ.get("LEAD_EXPORT_DIR")
                  or os.path.join(os.path.expanduser("~"), "ClaudeAssistant", "exports"))
    if not os.path.isdir(export_dir):
        # Refuse rather than create: a sheet written where nothing reads looks
        # exactly like success.
        print(f"[daily] export folder not found: {export_dir}", file=sys.stderr)
        print("[daily] set LEAD_EXPORT_DIR or pass --export-dir", file=sys.stderr)
        return 2

    try:
        config = load_config(args.config)
    except (OSError, ValueError) as exc:
        print(f"[daily] {exc}", file=sys.stderr)
        print(f"[daily] copy rotation.example.json to {args.config}", file=sys.stderr)
        return 2

    when = (dt.date.fromisoformat(args.date) if args.date else dt.date.today())
    state_path = args.state or os.path.join(HERE, STATE_FILE)

    status = run(config, export_dir, when, state_path, dry_run=args.dry_run)
    if not args.dry_run:
        # A dry run must not touch the status file: overwriting a real run's
        # record with "nothing was fetched" would erase the morning's report.
        write_status(export_dir, status)

    print_plan(config, status, when, export_dir)

    for sheet in status.get("sheets", []):
        print(f"[daily] {sheet['file']}: {sheet['rows']} rows")
    for problem in status.get("problems", []):
        print(f"[daily] PROBLEM: {problem}", file=sys.stderr)

    if status.get("note"):
        print(f"[daily] {status['note']}")
    return 1 if status.get("problems") else 0


if __name__ == "__main__":
    sys.exit(main())
