"""Pure exact-planning helpers for the sharded WebGPU merge.

These helpers deliberately have no SSH, GPU, torch, or topology dependencies so the
partition and U192 aggregation contracts can be checked in ordinary CI.
"""

U32_MAX = (1 << 32) - 1
U192_WORDS = 6


def allocate_slices(slices, total):
    """Allocate a contiguous, gap-free prefix cover subject to per-slice caps.

    ``slices`` is an ordered iterable of mappings with positive ``weight`` and
    non-negative ``max_prefixes``.  Returned jobs retain the input fields and add
    ``slice_start``/``slice_count``.  Zero-share slices are omitted.

    The weighted target is advisory.  Coverage and capacity are hard invariants.
    """
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ValueError("total must be a non-negative integer")

    items = []
    for index, raw in enumerate(slices):
        cap = raw.get("max_prefixes")
        weight = raw.get("weight")
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
            raise ValueError(f"slice {index} has invalid max_prefixes")
        if not isinstance(weight, int) or isinstance(weight, bool) or weight <= 0:
            raise ValueError(f"slice {index} has invalid weight")
        item = dict(raw)
        item["max_prefixes"] = cap
        item["weight"] = weight
        items.append(item)

    capacity = sum(item["max_prefixes"] for item in items)
    if capacity < total:
        raise ValueError(
            f"insufficient prefix capacity: {capacity} available for {total} required"
        )
    if total == 0:
        return []
    if not items:
        raise ValueError("no slices available for non-empty prefix space")

    remaining = total
    remaining_weight = sum(item["weight"] for item in items)
    cursor = 0
    jobs = []

    for index, item in enumerate(items):
        cap = item["max_prefixes"]
        weight = item["weight"]
        later_capacity = sum(x["max_prefixes"] for x in items[index + 1 :])

        # Reserve enough work for this slice when later slices cannot cover the
        # remainder, while otherwise following the current weighted target.
        minimum_here = max(0, remaining - later_capacity)
        maximum_here = min(cap, remaining)
        weighted_target = (
            remaining * weight // remaining_weight if remaining_weight else 0
        )
        share = max(minimum_here, min(maximum_here, weighted_target))

        # On the final slice the capacity precheck guarantees exact closure.
        if index == len(items) - 1:
            share = remaining

        if share:
            job = dict(item)
            job["slice_start"] = cursor
            job["slice_count"] = share
            jobs.append(job)
            cursor += share
            remaining -= share

        remaining_weight -= weight

    validate_partition(jobs, total)
    return jobs


def validate_partition(jobs, total):
    """Fail closed unless jobs form the ordered disjoint cover [0,total)."""
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise ValueError("total must be a non-negative integer")

    cursor = 0
    for index, job in enumerate(jobs):
        start = job.get("slice_start")
        count = job.get("slice_count")
        if not isinstance(start, int) or isinstance(start, bool):
            raise ValueError(f"job {index} has invalid slice_start")
        if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
            raise ValueError(f"job {index} has invalid slice_count")
        if start != cursor:
            raise ValueError(
                f"partition discontinuity at job {index}: expected start {cursor}, got {start}"
            )
        cursor += count
        if cursor > total:
            raise ValueError(f"partition exceeds prefix total at job {index}")

    if cursor != total:
        raise ValueError(f"incomplete prefix cover: {cursor}/{total}")
    return True


def u192_key(words):
    """Return the numeric comparison key for six little-endian u32 words."""
    if not isinstance(words, (list, tuple)) or len(words) != U192_WORDS:
        raise ValueError("U192 value must contain exactly six u32 words")
    checked = []
    for index, word in enumerate(words):
        if (
            not isinstance(word, int)
            or isinstance(word, bool)
            or word < 0
            or word > U32_MAX
        ):
            raise ValueError(f"word {index} is not a u32")
        checked.append(word)
    # Storage/output order is low word first; numeric lexicographic order is high
    # word first, matching merge.wgsl::lex_gt and merge.rs::words_to_biguint.
    return tuple(reversed(checked))


def u192_to_int(words):
    """Independent scalar interpretation of the six-word little-endian value."""
    key = u192_key(words)
    value = 0
    for word in key:
        value = (value << 32) | word
    return value


def aggregate_worker_results(results):
    """Return ``(max_words, source)`` using numeric U192 order."""
    best = None
    best_source = None
    best_key = None
    for source, result in results.items():
        words = result["max_words"]
        key = u192_key(words)
        if best_key is None or key > best_key:
            best = list(words)
            best_source = source
            best_key = key
    return best, best_source
