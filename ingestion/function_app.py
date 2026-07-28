"""Azure Functions adapter — thin wrapper only; all logic lives in sync.py.
Deployment is a later operator step; locally this module is inert (the
azure-functions package is not a local dependency)."""
try:
    import azure.functions as func
except ImportError:      # local dev / tests — no Functions runtime
    func = None

if func is not None:
    app = func.FunctionApp()

    @app.timer_trigger(schedule="0 */15 * * * *", arg_name="timer",
                       run_on_startup=False)
    def engagement_sync(timer: "func.TimerRequest") -> None:
        import json

        from .config import Config
        from .dataverse_client import DataverseClient
        from .graph_client import GraphClient
        from .sync import SyncRun, code_version

        cfg = Config.from_env()
        dv = DataverseClient(cfg.dataverse_url, cfg.tenant_id, cfg.client_id,
                             cfg.client_secret, cfg.prefix, apply=True)
        graph = GraphClient(cfg.tenant_id, cfg.client_id, cfg.client_secret)
        log = SyncRun(cfg, graph, dv, apply=True, codeversion=code_version()).run()
        print(json.dumps(log["counts"], default=str))
