"""Read-only: is the intake marker column live in PROD (intake active)?"""
from .config import Config
from .dataverse_client import DataverseClient


def main():
    cfg = Config.from_env()
    dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                         cfg.client_secret, cfg.prefix, apply=False)
    p = cfg.prefix
    for entity, col in (("contacts", f"{p}autocreatedby"),
                        (f"{p}inforequests", f"{p}thirdparty")):
        try:
            dv.query(f"{entity}?$select={col}&$top=1")
            print(f"OK   {entity}.{col} — present (feature active)")
        except Exception as e:
            print(f"MISSING  {entity}.{col} — {str(e)[:100]}")


if __name__ == "__main__":
    main()
