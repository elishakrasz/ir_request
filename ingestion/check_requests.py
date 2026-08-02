"""Quick read-only probe: info-request counts by status (dashboard sanity check)."""
from collections import Counter

from .config import Config
from .dataverse_client import DataverseClient


def main():
    cfg = Config.from_env()
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, cfg.prefix, apply=False)
    p = cfg.prefix
    rows = dv.query(f"{p}inforequests?$select={p}status,{p}receiveddate,{p}name")
    print("total requests:", len(rows))
    print(Counter(r.get(f"{p}status") for r in rows))
    for r in sorted(rows, key=lambda r: r.get(f"{p}receiveddate") or "")[-8:]:
        print(r.get(f"{p}receiveddate"), r.get(f"{p}status"),
              (r.get(f"{p}name") or "")[:60])


if __name__ == "__main__":
    main()
