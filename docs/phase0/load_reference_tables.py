"""Idempotent loader for the Phase-0 reference tables (new_ircategory, new_irrule)
from the CSVs beside this file into the CONFIGURED Dataverse env.

- Choice values (defaulttier / matchtype / action / tier) are resolved from LIVE
  option-set metadata — never hardcoded — so a base/order drift can't mis-map.
- The two systemuser lookups given as emails (defaulthandler, handler) are loaded
  UNBOUND; handlers are set separately (the ingestion app user has no AppendTo on
  systemuser). This mirrors how DEV was seeded.
- Re-runnable: rows already present (matched by the primary Name = the stable code
  / rule name) are skipped, so a second run is a no-op.

    venv/bin/python docs/phase0/load_reference_tables.py            # dry run
    venv/bin/python docs/phase0/load_reference_tables.py --apply    # writes

House rule: writing to PROD requires an explicit operator "commit to PROD".
"""
import argparse
import csv
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))   # repo root, so `ingestion` imports

from ingestion.config import Config, load_env
from ingestion.dataverse_client import DataverseClient


def option_map(dv, entity, attr) -> dict:
    """{option label -> integer value} for a local picklist column, from metadata."""
    path = (f"EntityDefinitions(LogicalName='{entity}')/Attributes("
            f"LogicalName='{attr}')/Microsoft.Dynamics.CRM.PicklistAttributeMetadata"
            "?$select=LogicalName&$expand=OptionSet($select=Options)")
    r = dv._req("GET", path)
    r.raise_for_status()
    opts = r.json()["OptionSet"]["Options"]
    return {o["Label"]["LocalizedLabels"][0]["Label"]: o["Value"] for o in opts}


def parse_bool(v: str) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y")


def existing_names(dv, entity_set, p) -> set:
    return {r[f"{p}name"] for r in dv.query(f"{entity_set}?$select={p}name")
            if r.get(f"{p}name")}


def _choice(body, attr, label, m, what):
    label = (label or "").strip()
    if not label:
        return
    if label not in m:
        raise SystemExit(f"unknown {what} {label!r} — env options are {sorted(m)}")
    body[attr] = m[label]


def load_categories(dv, p) -> tuple[int, int]:
    tier = option_map(dv, f"{p}ircategory", f"{p}defaulttier")
    have = existing_names(dv, f"{p}ircategories", p)
    made = skipped = 0
    with open(HERE / "new_ircategory.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            code = (row["code"] or "").strip()
            if not code:
                continue
            if code in have:
                skipped += 1
                continue
            body = {
                f"{p}name": code,
                f"{p}displayname": (row["name"] or "").strip() or code,
                f"{p}definition": (row["definition"] or "").strip(),
                f"{p}version": (row["version"] or "").strip(),
                f"{p}active": parse_bool(row["active"]),
                f"{p}sortorder": int(row["sortorder"]) if (row["sortorder"] or "").strip() else 0,
            }
            _choice(body, f"{p}defaulttier", row.get("defaulttier"), tier, "default tier")
            dv.create(f"{p}ircategories", body, f"ircategory {code}")
            made += 1
    return made, skipped


def load_rules(dv, p) -> tuple[int, int]:
    mt = option_map(dv, f"{p}irrule", f"{p}matchtype")
    ac = option_map(dv, f"{p}irrule", f"{p}action")
    ti = option_map(dv, f"{p}irrule", f"{p}tier")
    have = existing_names(dv, f"{p}irrules", p)
    made = skipped = 0
    with open(HERE / "new_irrule.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = (row["name"] or "").strip()
            if not name:
                continue
            if name in have:
                skipped += 1
                continue
            body = {
                f"{p}name": name,
                f"{p}matchvalue": (row["matchvalue"] or "").strip(),
                f"{p}priority": int(row["priority"]) if (row["priority"] or "").strip() else 0,
                f"{p}active": parse_bool(row["active"]),
                f"{p}note": (row["note"] or "").strip(),
            }
            _choice(body, f"{p}matchtype", row.get("matchtype"), mt, "match type")
            _choice(body, f"{p}action", row.get("action"), ac, "action")
            _choice(body, f"{p}tier", row.get("tier"), ti, "tier")
            dv.create(f"{p}irrules", body, f"irrule {name}")
            made += 1
    return made, skipped


def nav_prop(dv, rel_schema) -> str:
    """ReferencingEntityNavigationPropertyName for a lookup relationship — the key
    used in `<nav>@odata.bind`. Read from metadata, never guessed."""
    r = dv._req("GET",
                "RelationshipDefinitions/Microsoft.Dynamics.CRM."
                "OneToManyRelationshipMetadata?$select="
                f"ReferencingEntityNavigationPropertyName&$filter=SchemaName eq '{rel_schema}'")
    r.raise_for_status()
    v = r.json().get("value", [])
    if not v:
        raise SystemExit(f"relationship {rel_schema!r} not found")
    return v[0]["ReferencingEntityNavigationPropertyName"]


def resolve_users(dv, emails) -> dict:
    """{email(lowercased) -> systemuserid}, matching primary email OR login/UPN."""
    want = sorted({e.lower() for e in emails if e})
    out = {}
    for i in range(0, len(want), 15):
        chunk = want[i:i + 15]
        flt = " or ".join(
            [f"internalemailaddress eq '{e}'" for e in chunk]
            + [f"domainname eq '{e}'" for e in chunk])
        for u in dv.query("systemusers?$select=systemuserid,internalemailaddress,"
                          f"domainname&$filter={flt}"):
            for k in (u.get("internalemailaddress"), u.get("domainname")):
                if k:
                    out[k.lower()] = u["systemuserid"]
    return out


def _handler_map(csv_name, name_col, email_col) -> dict:
    out = {}
    with open(HERE / csv_name, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            nm, em = (row[name_col] or "").strip(), (row[email_col] or "").strip()
            if nm and em:
                out[nm] = em
    return out


def bind_handlers(dv, p) -> tuple[int, int]:
    """PATCH the systemuser lookups (defaulthandler / handler) on existing rows.
    Idempotent: a row already bound to the right user is left alone."""
    cats = _handler_map("new_ircategory.csv", "code", "defaulthandler")
    rules = _handler_map("new_irrule.csv", "name", "handler")
    users = resolve_users(dv, list(cats.values()) + list(rules.values()))
    missing = sorted({e.lower() for e in [*cats.values(), *rules.values()]} - set(users))
    if missing:
        raise SystemExit(f"unresolved handler emails (fix or create the users): {missing}")

    bound = skipped = 0
    for ent_set, id_col, lk, rel, src in (
        (f"{p}ircategories", f"{p}ircategoryid", f"{p}defaulthandler",
         f"{p}systemuser_ircategory_handler", cats),
        (f"{p}irrules", f"{p}irruleid", f"{p}handler",
         f"{p}systemuser_irrule_handler", rules),
    ):
        if not src:
            continue
        nav = nav_prop(dv, rel)
        for row in dv.query(f"{ent_set}?$select={p}name,{id_col},_{lk}_value"):
            em = src.get(row.get(f"{p}name"))
            if not em:
                continue
            uid = users[em.lower()]
            if row.get(f"_{lk}_value") == uid:
                skipped += 1
                continue
            dv.patch(ent_set, row[id_col], {f"{nav}@odata.bind": f"/systemusers({uid})"},
                     f"{lk} {row.get(f'{p}name')} -> {em}")
            bound += 1
    return bound, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    cfg = Config.from_env(load_env())
    p = cfg.prefix
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, cfg.prefix, apply=args.apply)
    print(f"{'APPLY (writing)' if args.apply else 'DRY RUN'} → {cfg.dataverse_url}")

    cn, cs = load_categories(dv, p)
    rn, rs = load_rules(dv, p)
    hb, hs = bind_handlers(dv, p)
    print(f"\ncategories : +{cn} to create, {cs} already present")
    print(f"rules      : +{rn} to create, {rs} already present")
    print(f"handlers   : {hb} to bind, {hs} already bound")
    if not args.apply:
        print("\n(dry run — nothing written; re-run with --apply)")


if __name__ == "__main__":
    main()
