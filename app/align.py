"""Core quote-alignment logic.

Matching is performed on whitespace-normalized text (every run of whitespace
is treated as a single space), while the returned offsets always refer to the
original, unmodified text.

Quotes are located jointly: the chosen spans must not overlap and must keep
the order of the quote array. Among all feasible selections we

1. maximize the number of located quotes,
2. minimize the total fuzzy edit distance (exact matches always cost 0),
3. minimize the total number of original characters skipped between located
   segments (characters before the first / after the last located segment do
   not count),
4. take the lexicographically smallest offset sequence.

Multi-document attribution (``attribute_quotes``) reuses the same
normalization and fuzzy candidate enumeration, but ranks selections by
located count, total edit distance, then the lexicographically smallest
(document index, start, end) sequence.

Excerpt alignment (``align_excerpts``) locates ordered chains of
fragments: every excerpt contributes a handful of fragments that must
appear in order without overlapping, leaving at most ``max_gap`` original
characters between adjacent fragments. Feasible fragment combinations are
enumerated per excerpt, then selected jointly across excerpts by located
count, total characters omitted inside excerpts, characters skipped
between excerpts, and finally the lexicographically smallest offset
sequence.
"""

from __future__ import annotations

import re
import sys
from bisect import bisect_left, bisect_right
from functools import lru_cache

_WS_RUN = re.compile(r"\s+")

# Fuzzy quotes may ask for at most this many edits; the banded Levenshtein
# enumeration is specialized to such small thresholds.
MAX_ALLOWED_EDITS = 2

# The dynamic program recurses once per quote; lift the default recursion
# limit so long quote arrays stay solvable.
sys.setrecursionlimit(max(sys.getrecursionlimit(), 100_000))


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Collapse every whitespace run to a single space.

    Returns the normalized string plus a list mapping each normalized
    character index to its index in the original text.
    """
    norm_chars: list[str] = []
    norm_to_orig: list[int] = []
    pos = 0
    for match in _WS_RUN.finditer(text):
        norm_chars.extend(text[pos : match.start()])
        norm_to_orig.extend(range(pos, match.start()))
        # The whole run becomes one space, mapped to the run's first char.
        norm_chars.append(" ")
        norm_to_orig.append(match.start())
        pos = match.end()
    norm_chars.extend(text[pos:])
    norm_to_orig.extend(range(pos, len(text)))
    return "".join(norm_chars), norm_to_orig


def _find_all(haystack: str, needle: str) -> list[int]:
    """All start offsets of ``needle`` in ``haystack``, including overlaps."""
    starts = []
    i = haystack.find(needle)
    while i != -1:
        starts.append(i)
        i = haystack.find(needle, i + 1)
    return starts


def _candidate_spans(
    norm_text: str, norm_to_orig: list[int], quote: str
) -> list[tuple[int, int]]:
    """Sorted original-text [start, end) spans where ``quote`` may be located."""
    norm_quote, _ = _normalize_with_map(quote)
    if not norm_quote.strip():
        # Empty or whitespace-only quotes carry no content to locate.
        return []
    spans = set()
    for start in _find_all(norm_text, norm_quote):
        end = start + len(norm_quote)
        spans.add((norm_to_orig[start], norm_to_orig[end - 1] + 1))
    return sorted(spans)


def _fuzzy_candidate_spans(
    norm_text: str, norm_to_orig: list[int], quote: str, max_edits: int
) -> list[tuple[int, int, int]]:
    """All ``(start, end, distance)`` fuzzy matches for one quote.

    Every non-empty normalized substring whose Unicode-code-point
    Levenshtein distance from the normalized quote is at most ``max_edits``
    is enumerated; offsets refer to the original text.
    """
    norm_quote, _ = _normalize_with_map(quote)
    if not norm_quote.strip():
        return []
    n = len(norm_quote)
    m = len(norm_text)
    if n > m + max_edits:
        return []

    # Any matching substring has length in [n-k, n+k]; non-empty only.
    min_len = max(1, n - max_edits)
    max_len = n + max_edits
    inf = max_edits + 1  # Costs above the threshold are equivalent.

    candidates: list[tuple[int, int, int]] = []
    for s in range(m):
        window_len = min(max_len, m - s)
        if window_len < min_len:
            break  # Later starts only get shorter windows.

        # Standard Levenshtein DP between the quote and text[s:s+j], run
        # once per start so all substring lengths share the same matrix.
        # Row 0: j insertions; cells outside the |i-j| <= k band already
        # exceed the threshold and are clamped to ``inf``.
        prev = [j if j <= max_edits else inf for j in range(window_len + 1)]
        completed = True
        for i in range(1, n + 1):
            cur = [inf] * (window_len + 1)
            if i <= max_edits:
                cur[0] = i  # Delete i quote characters.
            alive = i <= max_edits
            lo = max(1, i - max_edits)
            hi = min(window_len, i + max_edits)
            for j in range(lo, hi + 1):
                substitution_cost = (
                    0 if norm_quote[i - 1] == norm_text[s + j - 1] else 1
                )
                value = min(
                    prev[j] + 1,            # Deletion
                    cur[j - 1] + 1,         # Insertion
                    prev[j - 1] + substitution_cost,
                )
                if value <= max_edits:
                    cur[j] = value
                    alive = True
            if not alive:
                # Every further quote prefix can only add edits.
                completed = False
                break
            prev = cur

        if not completed:
            continue
        for j in range(min_len, window_len + 1):
            distance = prev[j]
            if distance <= max_edits:
                candidates.append(
                    (norm_to_orig[s], norm_to_orig[s + j - 1] + 1, distance)
                )
    return candidates


def _select_spans(
    candidates: list[list[tuple[int, int, int]]],
) -> list[tuple[int, int, int] | None]:
    """Jointly pick at most one span per quote.

    ``candidates[i]`` holds the feasible ``(start, end, cost)`` spans for
    quote ``i``, sorted by offset. Returns one span (or ``None``) per quote.
    Selection ranks by located count, then total cost, then skipped original
    characters between segments, then the lexicographically smallest offset
    sequence.
    """
    n = len(candidates)

    def rank(option: tuple[int, int, int, tuple]) -> tuple:
        count, cost, skip, seq = option
        return (-count, cost, skip, seq)

    @lru_cache(maxsize=None)
    def dp(i: int, prev_end: int) -> tuple[int, int, int, tuple]:
        # Best (located_count, total_cost, skipped_chars, span_sequence) for
        # quotes[i:], given the previously located segment ends at prev_end
        # (-1: none yet).
        if i == n:
            return (0, 0, 0, ())

        best = None
        for start, end, cost in candidates[i]:
            if prev_end != -1 and start < prev_end:
                continue
            count, rest_cost, skip, seq = dp(i + 1, end)
            gap = 0 if prev_end == -1 else start - prev_end
            option = (
                count + 1,
                rest_cost + cost,
                skip + gap,
                ((start, end),) + seq,
            )
            if best is None or rank(option) < rank(best):
                best = option

        # Leave quote i unlocated.
        option = dp(i + 1, prev_end)
        if best is None or rank(option) < rank(best):
            best = option
        return best

    # Reconstruct which candidate (if any) realizes the optimum per quote.
    result: list[tuple[int, int, int] | None] = [None] * n
    prev_end = -1
    for i in range(n):
        best = dp(i, prev_end)
        for start, end, cost in candidates[i]:
            if prev_end != -1 and start < prev_end:
                continue
            count, rest_cost, skip, seq = dp(i + 1, end)
            gap = 0 if prev_end == -1 else start - prev_end
            if (
                count + 1,
                rest_cost + cost,
                skip + gap,
                ((start, end),) + seq,
            ) == best:
                result[i] = (start, end, cost)
                prev_end = end
                break
    return result


def align_quotes(text: str, quotes: list[str]) -> list[tuple[int, int] | None]:
    """Locate every quote in ``text`` with exact (whitespace-insensitive) match.

    Returns a list parallel to ``quotes``; each entry is a ``(start, end)``
    pair of character offsets into the original text (end-exclusive), or
    ``None`` when the quote could not be located.
    """
    norm_text, norm_to_orig = _normalize_with_map(text)
    candidates = [
        [(start, end, 0) for start, end in _candidate_spans(
            norm_text, norm_to_orig, quote
        )]
        for quote in quotes
    ]
    selected = _select_spans(candidates)
    return [
        None if span is None else (span[0], span[1]) for span in selected
    ]


def _excerpt_combinations(
    fragment_candidates: list[list[tuple[int, int]]], max_gap: int
) -> list[tuple[int, int, int, tuple[tuple[int, int], ...]]]:
    """All feasible fragment-span combinations for one excerpt.

    ``fragment_candidates[j]`` holds the sorted original-text spans where
    fragment ``j`` may be located. A combination picks one span per
    fragment so that the spans keep fragment order, do not overlap, and
    leave a gap of at most ``max_gap`` original characters between
    adjacent fragments. Returns ``(start, end, omitted_chars,
    fragment_spans)`` tuples, where ``start``/``end`` span the whole
    excerpt and ``omitted_chars`` counts the original characters skipped
    between adjacent fragments.
    """
    k = len(fragment_candidates)
    starts = [
        [start for start, _ in candidates]
        for candidates in fragment_candidates
    ]
    chosen: list = [None] * k
    combinations = []

    def rec(j: int, prev_end: int) -> None:
        if j == k:
            fragment_spans = tuple(chosen)
            omitted = sum(
                fragment_spans[i + 1][0] - fragment_spans[i][1]
                for i in range(k - 1)
            )
            combinations.append(
                (
                    fragment_spans[0][0],
                    fragment_spans[-1][1],
                    omitted,
                    fragment_spans,
                )
            )
            return
        candidates = fragment_candidates[j]
        # Feasible starts lie in [prev_end, prev_end + max_gap]; the sorted
        # candidate list lets us jump straight to that window.
        lo = 0 if prev_end == -1 else bisect_left(starts[j], prev_end)
        hi = (
            len(candidates)
            if prev_end == -1
            else bisect_right(starts[j], prev_end + max_gap)
        )
        for index in range(lo, hi):
            chosen[j] = candidates[index]
            rec(j + 1, candidates[index][1])

    rec(0, -1)
    return combinations


def align_excerpts(
    text: str, excerpts: list[tuple[list[str], int]]
) -> list[
    tuple[int, int, tuple[tuple[int, int], ...], tuple[tuple[int, int], ...]]
    | None
]:
    """Locate every excerpt in ``text`` as an ordered chain of fragments.

    ``excerpts`` is a list of ``(fragments, max_gap)`` pairs. Each fragment
    is matched exactly (whitespace-insensitively); the fragments of an
    excerpt must appear in order, without overlapping, leaving a gap of at
    most ``max_gap`` original characters between adjacent fragments.
    Returns a list parallel to ``excerpts``; each entry is a ``(start,
    end, fragment_spans, omitted_ranges)`` tuple of character offsets into
    the original text (end-exclusive), or ``None`` when the excerpt could
    not be located. ``omitted_ranges`` holds the non-empty ranges skipped
    between adjacent fragments.

    Excerpts are located jointly: the chosen combinations must not overlap
    and must keep the excerpt array order. Among all feasible selections we

    1. maximize the number of located excerpts,
    2. minimize the total characters omitted inside the located excerpts,
    3. minimize the total characters skipped between located excerpts
       (characters before the first / after the last located excerpt do
       not count),
    4. take the lexicographically smallest offset sequence.
    """
    norm_text, norm_to_orig = _normalize_with_map(text)
    candidates = []
    combo_maps = []
    for fragments, max_gap in excerpts:
        fragment_candidates = [
            _candidate_spans(norm_text, norm_to_orig, fragment)
            for fragment in fragments
        ]
        # Combinations sharing the same outer span are interchangeable for
        # the joint selection except for their omitted count and inner
        # offsets, so keep only the cheapest (then earliest) one.
        best_by_outer: dict[tuple[int, int], tuple[int, tuple]] = {}
        for start, end, omitted, fragment_spans in _excerpt_combinations(
            fragment_candidates, max_gap
        ):
            current = best_by_outer.get((start, end))
            if current is None or (omitted, fragment_spans) < current:
                best_by_outer[(start, end)] = (omitted, fragment_spans)
        per_excerpt = []
        combo_map = {}
        for start, end in sorted(best_by_outer):
            omitted, fragment_spans = best_by_outer[(start, end)]
            per_excerpt.append((start, end, omitted))
            combo_map[(start, end, omitted)] = fragment_spans
        candidates.append(per_excerpt)
        combo_maps.append(combo_map)

    selected = _select_spans(candidates)
    results = []
    for span, combo_map in zip(selected, combo_maps):
        if span is None:
            results.append(None)
            continue
        fragment_spans = combo_map[span]
        omitted_ranges = tuple(
            (fragment_spans[i][1], fragment_spans[i + 1][0])
            for i in range(len(fragment_spans) - 1)
            if fragment_spans[i + 1][0] > fragment_spans[i][1]
        )
        results.append((span[0], span[1], fragment_spans, omitted_ranges))
    return results


def _select_spans_across_documents(
    candidates: list[list[tuple[int, int, int, int]]],
    num_documents: int,
) -> list[tuple[int, int, int, int] | None]:
    """Jointly pick at most one span per quote across several documents.

    ``candidates[i]`` holds the feasible ``(document_index, start, end,
    cost)`` spans for quote ``i``, sorted by document then offset. Spans
    chosen within the same document must keep quote order and not overlap;
    spans in different documents constrain each other only through the
    shared ranking. Selection ranks by located count, then total cost,
    then the lexicographically smallest (document, start, end) sequence.
    """
    n = len(candidates)

    def rank(option: tuple[int, int, tuple]) -> tuple:
        count, cost, seq = option
        return (-count, cost, seq)

    @lru_cache(maxsize=None)
    def dp(i: int, prev_ends: tuple[int, ...]) -> tuple[int, int, tuple]:
        # Best (located_count, total_cost, span_sequence) for quotes[i:],
        # where prev_ends[d] is the end of the last segment located in
        # document d (-1: none yet in that document).
        if i == n:
            return (0, 0, ())

        best = None
        for doc, start, end, cost in candidates[i]:
            if prev_ends[doc] != -1 and start < prev_ends[doc]:
                continue
            count, rest_cost, seq = dp(
                i + 1, prev_ends[:doc] + (end,) + prev_ends[doc + 1 :]
            )
            option = (
                count + 1,
                rest_cost + cost,
                ((doc, start, end),) + seq,
            )
            if best is None or rank(option) < rank(best):
                best = option

        # Leave quote i unattributed.
        option = dp(i + 1, prev_ends)
        if best is None or rank(option) < rank(best):
            best = option
        return best

    # Reconstruct which candidate (if any) realizes the optimum per quote.
    result: list[tuple[int, int, int, int] | None] = [None] * n
    prev_ends = (-1,) * num_documents
    for i in range(n):
        best = dp(i, prev_ends)
        for doc, start, end, cost in candidates[i]:
            if prev_ends[doc] != -1 and start < prev_ends[doc]:
                continue
            count, rest_cost, seq = dp(
                i + 1, prev_ends[:doc] + (end,) + prev_ends[doc + 1 :]
            )
            if (
                count + 1,
                rest_cost + cost,
                ((doc, start, end),) + seq,
            ) == best:
                result[i] = (doc, start, end, cost)
                prev_ends = prev_ends[:doc] + (end,) + prev_ends[doc + 1 :]
                break
    return result


def attribute_quotes(
    documents: list[str], quotes: list[tuple[str, int]]
) -> list[tuple[int, int, int, int] | None]:
    """Attribute every quote to at most one span in one of ``documents``.

    ``documents`` holds the original texts; their array order defines the
    document indices used for tie-breaking. ``quotes`` is a list of
    ``(quote_text, max_edits)`` pairs. Returns a list parallel to ``quotes``;
    each entry is a ``(document_index, start, end, edit_distance)`` tuple of
    character offsets into that document's original text (end-exclusive),
    or ``None`` when the quote could not be attributed.
    """
    normalized = [_normalize_with_map(text) for text in documents]
    candidates = []
    for quote, max_edits in quotes:
        per_quote: list[tuple[int, int, int, int]] = []
        for doc_index, (norm_text, norm_to_orig) in enumerate(normalized):
            per_quote.extend(
                (doc_index, start, end, distance)
                for start, end, distance in _fuzzy_candidate_spans(
                    norm_text, norm_to_orig, quote, max_edits
                )
            )
        per_quote.sort(key=lambda span: (span[0], span[1], span[2]))
        candidates.append(per_quote)
    return _select_spans_across_documents(candidates, len(documents))


def align_quotes_fuzzy(
    text: str, quotes: list[tuple[str, int]]
) -> list[tuple[int, int, int] | None]:
    """Locate every quote in ``text`` allowing bounded Levenshtein edits.

    ``quotes`` is a list of ``(quote_text, max_edits)`` pairs. Returns a list
    parallel to it; each entry is a ``(start, end, edit_distance)`` triple of
    character offsets into the original text (end-exclusive), or ``None`` when
    no substring within the edit threshold could be located.
    """
    norm_text, norm_to_orig = _normalize_with_map(text)
    candidates = [
        sorted(
            _fuzzy_candidate_spans(norm_text, norm_to_orig, quote, max_edits),
            key=lambda span: (span[0], span[1]),
        )
        for quote, max_edits in quotes
    ]
    return _select_spans(candidates)
