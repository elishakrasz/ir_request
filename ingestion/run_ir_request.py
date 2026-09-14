"""IR-request route (Option B) — restricted-scope request ingestion.

Reads ONLY the IR mailboxes (REQUEST_MAILBOXES, default ir@/mravid@/lgruber@)
over a short lookback (--months, default 3), spam-filtered by the WS1 noise
gate, CRM-contacts-only (intake auto-create OFF → mail from senders not in
Dynamics is dropped). This is the ONLY route that creates Information
Requests (cfg.create_requests=True); the broad engagement sync runs with
CREATE_REQUESTS=0 and the other mailboxes.

Graph auth uses a DEDICATED Mail.Read app (REQUEST_CLIENT_ID/SECRET/TENANT)
whose Graph access is locked to those 3 mailboxes by an Exchange Application
Access Policy. Until that app is set up, it falls back to the MAIN app so the
route can be dry-run-validated first. Dataverse writes always use the main
app (it holds the Dataverse application-user role; the restricted app is
Mail.Read only). Separate state dir → its own delta tokens, no clash with the
broad sync.

Usage:
  venv/bin/python -m ingestion.run_ir_request                 # dry run
  venv/bin/python -m ingestion.run_ir_request --apply --months 3
"""
import argparse
import dataclasses
import json
from datetime import datetime, timedelta, timezone

from .config import Config, STATE_DIR, load_env
from .dataverse_client import DataverseClient
from .graph_client import GraphClient
from .sync import SyncRun, code_version, say

DEFAULT_MAILBOXES = ("ir@exigentcap.com,mravid@exigentcap.com,"
                     "lgruber@exigentcap.com")
STATE = STATE_DIR / "ir_request"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--mailbox", action="append",
                    help="limit this run to specific mailbox(es) — used with "
                         "--days for a bounded first walk of a newly added one")
    ap.add_argument("--days", type=int, default=None,
                    help="ingest floor in DAYS, overriding --months. For bounded "
                         "re-walks: archive the delta tokens, then re-read with a "
                         "tight floor so only that window is processed (older "
                         "messages are dropped in _prep, before any enrichment "
                         "or classifier cost).")
    args = ap.parse_args()

    env = load_env()
    base = Config.from_env(env)
    mailboxes = [m.strip().lower() for m in
                 env.get("REQUEST_MAILBOXES", DEFAULT_MAILBOXES).split(",")
                 if m.strip()]
    if args.mailbox:
        mailboxes = [m.strip().lower() for m in args.mailbox]
    span = timedelta(days=args.days) if args.days is not None         else timedelta(days=30 * args.months)
    floor = datetime.now(timezone.utc) - span

    cfg = dataclasses.replace(
        base,
        mailboxes=mailboxes,
        intake_mailboxes=[],        # CRM-only: unknown senders dropped
        ingest_floor=floor,
        create_requests=True,       # the only route that opens tickets
        tag_signal_category=True,   # v3: category-tag every signal on this route
        suppress_third_party_requests=True,  # advisors/banks/custodians: no ticket
    )

    # Graph: dedicated restricted app if configured, else main app (dry-run).
    restricted = bool(env.get("REQUEST_CLIENT_ID"))
    g_tenant = env.get("REQUEST_TENANT_ID", base.tenant_id)
    g_client = env.get("REQUEST_CLIENT_ID", base.client_id)
    g_secret = env.get("REQUEST_CLIENT_SECRET", base.client_secret)
    graph = GraphClient(g_tenant, g_client, g_secret)

    # Dataverse always via the main app (holds the Dataverse app-user role).
    dv = DataverseClient(base.dataverse_url, base.tenant_id, base.client_id,
                         base.client_secret, base.prefix, apply=args.apply)

    say(f"IR-request route · mailboxes={mailboxes} · floor={floor:%Y-%m-%d} "
        f"({str(args.days) + 'd' if args.days is not None else str(args.months) + 'mo'}) · graph app={'RESTRICTED' if restricted else 'MAIN (dry-run validation)'} "
        f"· intake=off (CRM-only) · create_requests=on")
    if not restricted and args.apply:
        raise SystemExit("Refusing --apply on the MAIN app: set REQUEST_CLIENT_ID "
                         "(the restricted app) before a real request-route run, "
                         "so Graph access is enforced to the 3 mailboxes.")

    run = SyncRun(cfg, graph, dv, apply=args.apply, state_dir=STATE,
                  codeversion=code_version())
    log = run.run(mailboxes=mailboxes)

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"\n[{mode}] IR-request route {log['runid']} — counts:")
    print(json.dumps(log["counts"], indent=2, default=str))
    if not args.apply:
        creq = sum(1 for i in dv.intents if i.startswith("CREATE inforequest"))
        print(f"\nwould create ~{creq} Information Request(s) "
              f"from the 3 IR mailboxes (CRM contacts only, spam filtered)")


if __name__ == "__main__":
    main()
