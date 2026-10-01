"""逐笔分成的最小货币单位分配。

金额以最小货币单位（整数）存储与结算，例如元金额的"分"。
每笔播放独立分摊，保证逐笔总额守恒。
"""
from decimal import ROUND_FLOOR, Decimal, localcontext, getcontext

getcontext().prec = 40

ONE_HUNDRED = Decimal(100)


def allocate(amount: int, shares: dict[str, Decimal]) -> dict[str, int]:
    """把 amount 按 shares（holder_code -> 百分比, 合计 100）分为整数。

    规则：
      1. 每份按比例计算精确小数，先向下取整分配；
      2. 未分完的余数（小于权利人数量）按小数部分降序、
         权利人编号升序逐个补 1；
      3. 分摊结果之和严格等于 amount。
    """
    if amount < 0:
        raise ValueError("amount must be non-negative")

    total_pct = sum(shares.values(), Decimal(0))
    if total_pct != ONE_HUNDRED:
        raise ValueError(f"shares must total 100, got {total_pct}")

    if amount == 0:
        return {holder: 0 for holder in shares}

    floors: dict[str, int] = {}
    fractions: dict[str, Decimal] = {}

    with localcontext() as ctx:
        ctx.prec = 40
        for holder, pct in shares.items():
            exact = (Decimal(amount) * pct) / ONE_HUNDRED
            floor_val = int(exact.to_integral_value(rounding=ROUND_FLOOR))
            floors[holder] = floor_val
            fractions[holder] = exact - floor_val

    remainder = amount - sum(floors.values())

    # 余数一定 < 权利人数量；小数部分降序，相同则权利人编号升序
    order = sorted(fractions, key=lambda h: (-fractions[h], h))
    result = dict(floors)
    for holder in order[:remainder]:
        result[holder] += 1

    assert sum(result.values()) == amount, "allocation must conserve total"
    return result
