"""Read-only: find the Bongoni test opportunity and its delivery-relevant fields."""
from .config import Config
from .dataverse_client import DataverseClient


def main():
    cfg = Config.from_env()
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, cfg.prefix, apply=False)
    rows = dv.query(
        "opportunities?$select=opportunityid,name,new_live,new_prospectcode,"
        "modifiedon,statecode,_parentcontactid_value,mint_opportunitypipelinetypes"
        "&$filter=contains(name,'Bongoni')")
    if not rows:
        rows = dv.query(
            "opportunities?$select=opportunityid,name,new_live,new_prospectcode,"
            "modifiedon,statecode&$filter=contains(name,'Test')")
    for r in rows:
        print({k: v for k, v in r.items() if not k.startswith("@")})


if __name__ == "__main__":
    main()
