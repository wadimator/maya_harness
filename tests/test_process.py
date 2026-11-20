from maya_harness.process import affinity_mask, environment_block


def test_affinity_spares_the_first_cpus_above_eight() -> None:
    assert affinity_mask(16) == 0b1111_1111_1111_0000


def test_affinity_has_no_limit_up_to_eight_cpus() -> None:
    assert affinity_mask(8) == 0
    assert affinity_mask(4) == 0


def test_environment_block_is_sorted_and_double_terminated() -> None:
    block = environment_block({"b": "2", "A": "1", "C": "3"})
    assert block == "A=1\0b=2\0C=3\0\0"
