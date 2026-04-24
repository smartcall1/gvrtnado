import pytest
from pair_manager import PairManager


def test_common_pairs():
    pm = PairManager(default_pair="BTC")
    pm.set_available_pairs(
        nado=["BTC", "ETH", "SOL", "BNB", "XRP"],
        grvt=["BTC", "ETH", "SOL"],
    )
    assert pm.common_pairs == {"BTC", "ETH", "SOL"}


def test_boost_config():
    pm = PairManager(default_pair="BTC")
    pm.set_available_pairs(nado=["BTC", "ETH"], grvt=["BTC", "ETH"])
    pm.set_boost("BTC", nado=4.0, grvt=1.0)
    pm.set_boost("ETH", nado=1.0, grvt=3.0)
    assert pm.get_boost("BTC") == {"nado": 4.0, "grvt": 1.0}
    assert pm.get_boost("ETH") == {"nado": 1.0, "grvt": 3.0}


def test_score_with_boost():
    pm = PairManager(default_pair="BTC")
    pm.set_available_pairs(nado=["BTC", "ETH"], grvt=["BTC", "ETH"])
    pm.set_boost("BTC", nado=4.0, grvt=1.0)
    pm.set_boost("ETH", nado=1.0, grvt=3.0)
    funding = {"BTC": 0.001, "ETH": 0.002}
    liquidity = {"BTC": 100000, "ETH": 50000}
    best = pm.best_pair(funding_spreads=funding, liquidities=liquidity, min_liquidity=10000)
    assert best in ("BTC", "ETH")


def test_score_excludes_low_liquidity():
    pm = PairManager(default_pair="BTC")
    pm.set_available_pairs(nado=["BTC", "ETH"], grvt=["BTC", "ETH"])
    pm.set_boost("ETH", nado=10.0, grvt=10.0)
    funding = {"BTC": 0.001, "ETH": 0.005}
    liquidity = {"BTC": 100000, "ETH": 500}
    best = pm.best_pair(funding_spreads=funding, liquidities=liquidity, min_liquidity=10000)
    assert best == "BTC"


def test_parse_boost_env():
    pm = PairManager(default_pair="BTC")
    pm.set_available_pairs(nado=["BTC", "ETH"], grvt=["BTC", "ETH"])
    pm.parse_boost_string("BTC:4x,ETH:3x")
    assert pm.get_boost("BTC") == {"nado": 4.0, "grvt": 4.0}
    assert pm.get_boost("ETH") == {"nado": 3.0, "grvt": 3.0}


def test_clear_boost():
    pm = PairManager(default_pair="BTC")
    pm.set_available_pairs(nado=["BTC"], grvt=["BTC"])
    pm.set_boost("BTC", nado=4.0, grvt=1.0)
    pm.clear_boost()
    assert pm.get_boost("BTC") == {"nado": 1.0, "grvt": 1.0}
