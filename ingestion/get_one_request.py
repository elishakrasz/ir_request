"""Read-only: print one open request's id + status (for the write-path test)."""
from .config import Config
from .dataverse_client import DataverseClient


def main():
    cfg = Config.from_env()
    p = cfg.prefix
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, p, apply=False)
    rows = dv.query(f"{p}inforequests?$select={p}name,{p}status,"
                    f"{p}modifiedbyhint&$top=1")
    for r in rows:
        print(r[f"{p}inforequestid"], "|",
              r.get(f"{p}status@OData.Community.Display.V1.FormattedValue"),
              "|", (r.get(f"{p}name") or "")[:50],
              "| hint:", r.get(f"{p}modifiedbyhint"))


if __name__ == "__main__":
    main()
