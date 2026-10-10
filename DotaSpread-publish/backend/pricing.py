"""Money calculations use integer minor units and Decimal, never binary floats."""
from decimal import Decimal, ROUND_HALF_UP


def cents(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def steam_total(seller_cents):
    return seller_cents + max(1, seller_cents * 5 // 100) + max(1, seller_cents * 10 // 100)


def steam_net(buyer_cents):
    """Invert the buyer total, including both minimum fees and rounding.

    Any unallocatable final cent remains in the fees, as on the Steam market.
    USD and EUR use a minimum of one cent for each of the two fees.
    """
    low, high = 0, max(0, buyer_cents)
    while low < high:
        mid = (low + high + 1) // 2
        if steam_total(mid) <= buyer_cents:
            low = mid
        else:
            high = mid - 1
    return low


def profitability(cost_usd, bid_cents, source_usd_rate, deposit_fee_percent=0):
    cost = Decimal(str(cost_usd)) * (1 + Decimal(str(deposit_fee_percent)) / 100)
    rate = Decimal(str(source_usd_rate))
    if cost <= 0 or rate <= 0:
        raise ValueError('Cost and conversion rate must be positive')
    gross = Decimal(bid_cents) / 100 * rate
    net = Decimal(steam_net(bid_cents)) / 100 * rate
    profit = net - cost
    return {'cost': cost, 'gross': gross, 'net': net, 'profit': profit, 'roi': profit / cost * 100}
