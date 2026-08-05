"""Read-only: confirm the close-readiness columns exist in PROD after the
manual solution import (a $select on a missing column returns 400)."""
from .config import Config
from .dataverse_client import DataverseClient


def main():
    cfg = Config.from_env()
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, cfg.prefix, apply=False)
    p = cfg.prefix
    from collections import Counter
    FMT = "@OData.Community.Display.V1.FormattedValue"
    for entity, col, use_fmt in (
            (f"{p}engagementsignals", f"{p}isprimary", False),
            (f"{p}inforequests", f"{p}routingcategory", True)):
        try:
            rows = dv.query(f"{entity}?$select={col}")
            key = f"{col}{FMT}" if use_fmt else col
            print(f"OK   {entity}.{col}  "
                  f"{dict(Counter(str(r.get(key, r.get(col))) for r in rows))}")
        except Exception as e:
            print(f"MISSING  {entity}.{col}  → {str(e)[:120]}")


if __name__ == "__main__":
    main()
