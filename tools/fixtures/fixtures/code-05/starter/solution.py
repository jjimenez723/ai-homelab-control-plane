def retry(fn, attempts=3, delay=0.0, exceptions=(Exception,)):
    # TODO: call fn() up to `attempts` times, retrying while it raises one
    # of `exceptions`, waiting `delay` seconds between attempts. Return the
    # first successful result. Re-raise the last exception if every attempt
    # fails. Do not touch the filesystem.
    raise NotImplementedError
