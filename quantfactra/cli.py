"""Public framework commands; application research lives in separate private projects."""
from __future__ import annotations
import argparse
import json
import sys

def main(argv=None, *, prog="quantfactra"):
    if callable(getattr(sys.stdout,"reconfigure",None)):
        sys.stdout.reconfigure(encoding="utf-8")
    parser=argparse.ArgumentParser(prog=prog,description=__doc__)
    commands=parser.add_subparsers(dest="command",required=True)
    for name,description in (("demo","Offline execution adapter rehearsal"),("init","Editable platform and AI integration starter"),("case","Public ECB example on optional bt")):
        p=commands.add_parser(name,help=description);p.add_argument("--output",required=True)
        if name=="case":p.add_argument("--resume",action="store_true")
    factor=commands.add_parser("factor-case",help="Published French momentum return reproduction")
    factor.add_argument("--output",required=True)
    source=factor.add_mutually_exclusive_group();source.add_argument("--source");source.add_argument("--download",action="store_true")
    factor.add_argument("--resume",action="store_true")
    team=commands.add_parser("team",help="Offline multi-agent coordination rehearsals")
    modes=team.add_subparsers(dest="mode",required=True)
    for name in ("demo","host-demo"):
        p=modes.add_parser(name);p.add_argument("--output",required=True)
    args=parser.parse_args(argv)
    try:
        if args.command=="demo":
            from .demo import run_demo
            result=run_demo(args.output)
        elif args.command=="init":
            from .onboarding import initialize
            result=initialize(args.output)
        elif args.command=="case":
            from .public_case import run_case
            result=run_case(args.output,resume_existing=args.resume)
        elif args.command=="factor-case":
            from .published_factor import run_factor_case
            result=run_factor_case(args.output,source_path=args.source,download=args.download,resume_existing=args.resume)
        elif args.mode=="host-demo":
            from panda_alpha.team.rehearsal import run_rehearsal
            result=run_rehearsal(args.output)
        else:
            from panda_alpha.team.demo import run_demo
            result=run_demo(args.output)
    except FileExistsError:
        parser.error(f"Output directory already exists: {args.output}. Choose a new --output path; existing files were preserved.")
    except (ValueError,RuntimeError,FileNotFoundError) as error:
        parser.error(str(error))
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
