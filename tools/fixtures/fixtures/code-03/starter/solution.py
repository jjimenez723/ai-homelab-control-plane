def chunk(items, size):
    # BUG: off-by-one in the slice upper bound drops the last element of
    # every chunk. Fix the loop below so each chunk has up to `size` items.
    result = []
    for i in range(0, len(items), size):
        result.append(items[i:i + size - 1])
    return result
