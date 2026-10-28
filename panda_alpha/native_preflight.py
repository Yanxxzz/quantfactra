"""Private application preflights are deliberately not bundled with the SDK."""
def verify_native_preflight(*args, **kwargs):
    raise ValueError('Supply your own preflight through a quantfactra-v1 backend. The public framework does not bundle application Python factor readers.')
