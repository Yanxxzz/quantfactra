"""Applications own their data collectors; the framework starts with none."""
def validate(spec):
    if spec.get('data_requests'):
        raise ValueError('Connect application data through your own worker; no collectors are bundled')

def material(*args, **kwargs):
    return []

def refresh(*args, **kwargs):
    return []
