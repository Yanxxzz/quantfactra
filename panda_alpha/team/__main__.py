"""Explicit generic supervisor entry point, without private research commands."""
import argparse
import json
from .runtime import serve

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    server=commands.add_parser('host-serve')
    for field in ('ledger','registry','session','workers','output'):server.add_argument('--'+field,required=True)
    args=parser.parse_args(argv)
    result=serve(args.ledger,args.registry,args.session,args.workers,args.output)
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
