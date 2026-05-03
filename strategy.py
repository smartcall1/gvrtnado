from typing import Optional


def normalize_funding_to_8h(rate: float, period_hours: int) -> float:
    return rate * (8 / period_hours)


def decide_direction(nado_8h: float, grvt_8h: float, min_spread: float = 0.0005) -> Optional[str]:
    net_a = grvt_8h - nado_8h
    net_b = nado_8h - grvt_8h
    best = max(net_a, net_b)
    if best <= 0 or best < min_spread:
        return None
    return "A" if net_a >= net_b else "B"


def should_exit_cycle(
    hold_hours: float,
    min_hold_hours: float,
    max_hold_days: int,
    margin_ratio: float,
    margin_emergency: float,
) -> Optional[str]:
    if margin_ratio <= margin_emergency:
        return "margin_emergency"
    if hold_hours < min_hold_hours:
        return None
    if hold_hours >= max_hold_days * 24:
        return "max_hold"
    return None


def should_exit_spread(
    spread_mtm: float,
    threshold: float,
    stoploss: float = -30.0,
) -> bool:
    if spread_mtm >= threshold:
        return True
    if spread_mtm <= stoploss:
        return True
    return False


def is_opposite_direction_better(
    current: str,
    nado_8h: float,
    grvt_8h: float,
    threshold: float = 0.0005,
) -> bool:
    if current == "A":
        current_net = grvt_8h - nado_8h
        opposite_net = nado_8h - grvt_8h
    else:
        current_net = nado_8h - grvt_8h
        opposite_net = grvt_8h - nado_8h
    return opposite_net - current_net > threshold


def calc_notional(
    nado_balance: float,
    grvt_balance: float,
    leverage: int,
    margin_buffer: float = 0.95,
) -> float:
    return min(nado_balance, grvt_balance) * leverage * margin_buffer


def determine_mode(
    volume_met: bool,
    trades_met: bool,
    days_left: int,
    volume_remaining: float,
    daily_capacity: float,
) -> str:
    if not trades_met:
        return "VOLUME_URGENT"
    if volume_met:
        return "HOLD"
    daily_needed = volume_remaining / max(days_left, 1)
    if daily_needed > daily_capacity * 0.7:
        return "VOLUME_URGENT"
    return "VOLUME"


def is_entry_favorable(direction: str, nado_price: float, grvt_price: float) -> bool:
    if direction == "A":
        return nado_price <= grvt_price
    return nado_price >= grvt_price
