def summarize(numbers):
    numbers = list(numbers)
    total = sum(numbers)
    count = len(numbers)
    return {"count": count, "sum": total, "avg": (total / count) if count else 0}


def summarize_json(numbers):
    # TODO: return summarize(numbers) serialized as a JSON string.
    raise NotImplementedError
