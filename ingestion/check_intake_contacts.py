"""Read-only: list auto-created intake contacts (new_autocreatedby set)."""
from .config import Config
from .dataverse_client import DataverseClient


def main():
    cfg = Config.from_env()
    p = cfg.prefix
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, p, apply=False)
    rows = dv.query(f"contacts?$select=fullname,emailaddress1,{p}autocreatedby,"
                    f"createdon&$filter={p}autocreatedby ne null")
    print(f"{len(rows)} auto-created intake contact(s):")
    for r in rows:
        print(f"  {r.get('fullname')} <{r.get('emailaddress1')}> "
              f"created {str(r.get('createdon'))[:16]} "
              f"({r.get(f'{p}autocreatedby')})")


if __name__ == "__main__":
    main()
