"""无外部依赖的单元测试：python3 -m unittest tests.test_allocation"""
import unittest
from decimal import Decimal

from app.allocation import allocate


class TestAllocate(unittest.TestCase):
    def test_zero_amount(self):
        self.assertEqual(
            allocate(0, {"A": Decimal("60"), "B": Decimal("40")}),
            {"A": 0, "B": 0},
        )

    def test_even_split(self):
        self.assertEqual(
            allocate(100, {"A": Decimal("50"), "B": Decimal("50")}),
            {"A": 50, "B": 50},
        )

    def test_largest_remainder_three_holders(self):
        # 33.3333 / 33.3333 / 33.3334，1 分：floor 全 0，余数 1
        # C 小数部分 .333334 最大 => C 拿 1；编号仅在小数部分相同时才决定次序
        result = allocate(
            1,
            {
                "A": Decimal("33.3333"),
                "B": Decimal("33.3333"),
                "C": Decimal("33.3334"),
            },
        )
        self.assertEqual(sum(result.values()), 1)
        self.assertEqual(result, {"A": 0, "B": 0, "C": 1})

    def test_tie_broken_by_holder_code(self):
        # 50/50 分 1：小数部分相同，编号升序 => a 拿 1
        result = allocate(1, {"b": Decimal("50"), "a": Decimal("50")})
        self.assertEqual(result, {"a": 1, "b": 0})

    def test_conservation_many_cases(self):
        shares = {"H1": Decimal("62.5"), "H2": Decimal("30"), "H3": Decimal("7.5")}
        for amount in range(0, 500):
            parts = allocate(amount, shares)
            self.assertEqual(sum(parts.values()), amount, amount)
            self.assertTrue(all(v >= 0 for v in parts.values()))

    def test_remainder_distribution_known_value(self):
        # 10 分按 62.5/30/7.5：精确 6.25 / 3.0 / 0.75 => floor 6/3/0，余 1
        # 小数部分：H1 .25、H3 .75、H2 .0 => H3 补 1 => 6/3/1
        self.assertEqual(
            allocate(10, {"H1": Decimal("62.5"), "H2": Decimal("30"), "H3": Decimal("7.5")}),
            {"H1": 6, "H2": 3, "H3": 1},
        )

    def test_rejects_bad_total(self):
        with self.assertRaises(ValueError):
            allocate(10, {"A": Decimal("50"), "B": Decimal("49")})


if __name__ == "__main__":
    unittest.main()
