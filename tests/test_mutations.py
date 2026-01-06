from swe_mutation.mutations import (
    mutate_flip_comparisons,
    mutate_negate_ifs,
    mutate_swap_plus_minus,
)


SAMPLE = """
def f(x):
    if x > 10:
        return x + 1
    elif x == 5:
        return x - 2
    else:
        return x
"""


def test_flip_comparisons_changes_code():
    mutated = mutate_flip_comparisons(SAMPLE)
    assert mutated != SAMPLE
    compile(mutated, "<mutated>", "exec")


def test_negate_ifs_changes_code():
    mutated = mutate_negate_ifs(SAMPLE)
    assert mutated != SAMPLE
    compile(mutated, "<mutated>", "exec")


def test_swap_plus_minus_changes_code():
    mutated = mutate_swap_plus_minus(SAMPLE)
    assert mutated != SAMPLE
    compile(mutated, "<mutated>", "exec")

