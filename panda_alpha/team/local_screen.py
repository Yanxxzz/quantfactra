"""Explicit local-screen plugins, with no bundled strategies or datasets."""
import importlib

class LocalScreenBlocked(ValueError):
    pass

def local_first(board, task, config, registry, *, require_semantics=False):
    policy=config.get('local_screen',{})
    if policy.get('enabled') is not True:
        return None
    adapter=policy.get('adapter','')
    if ':' not in adapter:
        raise LocalScreenBlocked('Enable local screening only with your own module:function adapter')
    module, name=adapter.split(':',1)
    callback=getattr(importlib.import_module(module),name)
    return callback(board,task,config,registry,require_semantics=require_semantics)
