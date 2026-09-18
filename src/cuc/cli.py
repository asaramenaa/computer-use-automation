"""cuc command line.

  cuc target-app serve                      run the legacy stand-in app
  cuc discover --goal ... --target ...      LLM-driven discovery -> artifact
  cuc replay --artifact ... --params ...    deterministic replay (no LLM)
  cuc resume --run-id ... --decision ...    operator signal during a handoff
  cuc catalog list | invoke                 capabilities as typed tools
  cuc schema export                         write JSON Schema to /schema
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(os.environ.get("CUC_ROOT", Path.cwd()))


def _paths() -> dict[str, Path]:
    return {"artifacts": ROOT / "artifacts", "runs": ROOT / "runs", "policy": Path(os.environ.get("CUC_POLICY", ROOT / "policy.yaml")),
            "profiles": ROOT / "app_profiles", "schema": ROOT / "schema"}


def _parse_params(items: list[str], json_text: str | None) -> dict:
    params = json.loads(json_text) if json_text else {}
    for it in items or []:
        k, _, v = it.partition("=")
        params[k.strip()] = v
    return params


# ------------------------------------------------------------------ commands
def cmd_target_app(args: argparse.Namespace) -> int:
    from cuc.target_app import create_app

    port = args.port or int(os.environ.get("TARGET_APP_PORT", "5055"))
    print(f"target app on http://127.0.0.1:{port}/  (seeded operators and members: see README)")
    create_app().run(host="127.0.0.1", port=port, debug=False, threaded=True)
    return 0


def _common(args: argparse.Namespace, run_prefix: str):
    from cuc.observability import RunLog, new_run_id
    from cuc.policy import Policy, Redactor, SecretStore
    from cuc.surface.playwright_surface import PlaywrightSurface

    p = _paths()
    policy = Policy.load(p["policy"])
    redactor = Redactor(policy.sensitive_keys)
    run_id = args.run_id or new_run_id(run_prefix)
    run_dir = p["runs"] / run_id
    log = RunLog(run_dir, run_id, redactor)
    surface = PlaywrightSurface(headless=args.headless, viewport=(1280, 800))
    return p, policy, redactor, run_id, run_dir, log, surface, SecretStore(redactor)


def _handler(args: argparse.Namespace, surface, log, run_dir):
    if not getattr(args, "escalate", False):
        return None
    if args.headless:
        print("warning: --escalate with --headless: the human cannot see the session; use a headed browser for handoffs", file=sys.stderr)
    from cuc.handoff import CliEscalationHandler

    return CliEscalationHandler(surface, log, run_dir, timeout_s=float(os.environ.get("CUC_HANDOFF_TIMEOUT_S", "900")))


def cmd_discover(args: argparse.Namespace) -> int:
    from cuc.agent.llm import make_client
    from cuc.agent.loop import AgentLoop
    from cuc.agent.recorder import AppProfile, build_artifact

    p, policy, redactor, run_id, run_dir, log, surface, secrets = _common(args, "discover")
    secret_refs = dict(s.split("=", 1) for s in (args.secret or []))
    profile = AppProfile.load(p["profiles"] / f"{args.app_profile}.yaml")
    try:
        llm = make_client()
        loop = AgentLoop(surface, llm, policy, log, secrets, run_dir, secret_refs=secret_refs, max_steps=args.max_steps,
                         timeout_s=args.timeout, escalation=_handler(args, surface, log, run_dir), screenshots=True)
        print(f"run {run_id}: {llm.provider}:{llm.model}  log={log.path}")
        outcome = loop.run(args.goal, args.target)
        print(f"discovery finished: {outcome.status} ({outcome.steps_used} actions, tokens {outcome.usage}) - {outcome.summary}")
        if outcome.status != "goal_met":
            return 2
        artifact = build_artifact(outcome, args.goal, args.target, profile, run_id, f"{llm.provider}:{llm.model}", redactor,
                                  capability_id=args.capability_id)
        p["artifacts"].mkdir(exist_ok=True)
        out = p["artifacts"] / f"{artifact.capability_id}.json"
        out.write_text(artifact.model_dump_json(indent=2) + "\n")
        log.event("artifact_written", path=str(out), capability_id=artifact.capability_id, steps=len(artifact.steps),
                  inputs=[i.name for i in artifact.inputs], outputs=[o.name for o in artifact.outputs])
        print(f"artifact: {out}\n  inputs: {[i.name for i in artifact.inputs]}\n  outputs: {[o.name for o in artifact.outputs]}\n  steps: {len(artifact.steps)}")
        return 0
    finally:
        log.close()
        surface.close()


def _replay(args: argparse.Namespace, artifact, params: dict) -> int:
    from cuc.replay import ReplayEngine

    p, policy, redactor, run_id, run_dir, log, surface, secrets = _common(args, "replay")
    try:
        engine = ReplayEngine(surface, policy, log, secrets, run_dir, escalation=_handler(args, surface, log, run_dir))
        print(f"run {run_id}: replaying {artifact.capability_id}@{artifact.version} params={params}  log={log.path}")
        result = engine.run(artifact, params)
        print(json.dumps(result.model_dump(mode="json"), indent=2))
        return {"SUCCESS": 0, "BUSINESS_OUTCOME": 0, "ESCALATED": 3, "FAILED": 1}[result.status.value]
    finally:
        log.close()
        surface.close()


def cmd_replay(args: argparse.Namespace) -> int:
    from cuc.schema import Artifact

    artifact = Artifact.model_validate_json(Path(args.artifact).read_text())
    return _replay(args, artifact, _parse_params(args.param, args.params))


def cmd_resume(args: argparse.Namespace) -> int:
    from cuc.handoff import LeaseFile

    lease = LeaseFile(_paths()["runs"] / args.run_id).signal_resume(args.decision, note=args.note or "", by=os.environ.get("USER", "operator"))
    print(f"signalled {args.decision} for run {args.run_id} (lease v{lease.version}); automation will re-verify and continue")
    return 0


def cmd_catalog(args: argparse.Namespace) -> int:
    from cuc.catalog import Catalog

    cat = Catalog(_paths()["artifacts"])
    if args.catalog_cmd == "list":
        tools = cat.tools()
        if args.as_json:
            print(json.dumps([t.as_tool_definition() for t in tools], indent=2))
        else:
            for t in tools:
                print(f"{t.name}@{t.version}\n  {t.description}\n  inputs:  {list(t.input_schema['properties'])}\n  outputs: {list(t.output_schema['properties'])}"
                      f"\n  outcomes: {t.outcome_codes}\n  irreversible: {t.irreversible}\n  file: {t.path}")
        return 0
    params = _parse_params(args.param, args.params)
    rc = {"code": 0}

    def runner(artifact, p):
        rc["code"] = _replay(args, artifact, p)
        return None

    cat.invoke(args.name, params, runner)
    return rc["code"]


def cmd_schema(args: argparse.Namespace) -> int:
    from cuc.schema.export import export

    for path in export(_paths()["schema"]):
        print(f"wrote {path}")
    return 0


# --------------------------------------------------------------------- main
def _add_browser_flags(sp: argparse.ArgumentParser, escalate: bool = True) -> None:
    sp.add_argument("--headless", action="store_true", help="run the browser headless (default: headed so a human can watch/take over)")
    sp.add_argument("--run-id", help="run id (default: generated)")
    if escalate:
        sp.add_argument("--escalate", action="store_true", help="hand the live session to a human when stuck or at an irreversible step")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cuc", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("target-app", help="legacy stand-in app").add_subparsers(dest="target_cmd", required=True)
    s = t.add_parser("serve")
    s.add_argument("--port", type=int)
    s.set_defaults(fn=cmd_target_app)

    d = sub.add_parser("discover", help="LLM-driven discovery run that records an artifact")
    d.add_argument("--goal", required=True)
    d.add_argument("--target", required=True, help="entry URL")
    d.add_argument("--app-profile", default="core_serv", help="product profile in app_profiles/")
    d.add_argument("--secret", action="append", help="NAME=description; the model may type {{secret:NAME}}, value comes from env NAME")
    d.add_argument("--max-steps", type=int, default=int(os.environ.get("CUC_MAX_STEPS", "20")))
    d.add_argument("--timeout", type=float, default=600)
    d.add_argument("--capability-id", help="override the id the model proposes")
    _add_browser_flags(d)
    d.set_defaults(fn=cmd_discover)

    r = sub.add_parser("replay", help="deterministic replay of an artifact (no LLM)")
    r.add_argument("--artifact", required=True)
    r.add_argument("--param", action="append", help="name=value (repeatable)")
    r.add_argument("--params", help="JSON object of params")
    _add_browser_flags(r)
    r.set_defaults(fn=cmd_replay)

    z = sub.add_parser("resume", help="operator: hand control back to automation")
    z.add_argument("--run-id", required=True)
    z.add_argument("--decision", choices=["resumed", "confirmed", "abandoned"], default="resumed")
    z.add_argument("--note")
    z.set_defaults(fn=cmd_resume)

    c = sub.add_parser("catalog", help="capabilities as typed tools").add_subparsers(dest="catalog_cmd", required=True)
    cl = c.add_parser("list")
    cl.add_argument("--json", dest="as_json", action="store_true")
    cl.set_defaults(fn=cmd_catalog)
    ci = c.add_parser("invoke")
    ci.add_argument("name")
    ci.add_argument("--param", action="append")
    ci.add_argument("--params")
    _add_browser_flags(ci)
    ci.set_defaults(fn=cmd_catalog)

    sc = sub.add_parser("schema").add_subparsers(dest="schema_cmd", required=True)
    sc.add_parser("export").set_defaults(fn=cmd_schema)
    return ap


def main(argv: list[str] | None = None) -> int:
    load_dotenv(ROOT / ".env")
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
