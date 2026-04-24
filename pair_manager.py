import math
import logging

logger = logging.getLogger(__name__)


class PairManager:
    def __init__(self, default_pair: str = "BTC"):
        self._default = default_pair
        self._nado_pairs: set[str] = set()
        self._grvt_pairs: set[str] = set()
        self._boosts: dict[str, dict[str, float]] = {}

    @property
    def common_pairs(self) -> set[str]:
        return self._nado_pairs & self._grvt_pairs

    def set_available_pairs(self, nado: list[str], grvt: list[str]):
        self._nado_pairs = set(nado)
        self._grvt_pairs = set(grvt)

    def set_boost(self, pair: str, nado: float = 1.0, grvt: float = 1.0):
        self._boosts[pair] = {"nado": nado, "grvt": grvt}

    def get_boost(self, pair: str) -> dict[str, float]:
        return self._boosts.get(pair, {"nado": 1.0, "grvt": 1.0})

    def clear_boost(self):
        self._boosts.clear()

    def parse_boost_string(self, s: str):
        if not s or s.strip().lower() == "clear":
            self.clear_boost()
            return
        for token in s.split(","):
            token = token.strip()
            if ":" not in token:
                continue
            pair, mult_str = token.split(":", 1)
            mult = float(mult_str.strip().lower().replace("x", ""))
            self.set_boost(pair.strip().upper(), nado=mult, grvt=mult)

    def best_pair(
        self,
        funding_spreads: dict[str, float],
        liquidities: dict[str, float],
        min_liquidity: float = 75000,
    ) -> str:
        candidates = self.common_pairs
        if not candidates:
            return self._default

        best_pair = self._default
        best_score = -999.0

        for pair in candidates:
            liq = liquidities.get(pair, 0)
            if liq < min_liquidity:
                continue
            boost = self.get_boost(pair)
            fund = funding_spreads.get(pair, 0)
            score = (
                (boost["nado"] + boost["grvt"]) * 3.0
                + fund * 1000
                + math.log(max(liq, 1)) * 0.5
            )
            if score > best_score:
                best_score = score
                best_pair = pair

        return best_pair

    def to_dict(self) -> dict:
        return {
            "boosts": self._boosts.copy(),
            "nado_pairs": sorted(self._nado_pairs),
            "grvt_pairs": sorted(self._grvt_pairs),
        }

    def load_boosts(self, d: dict):
        self._boosts = d.get("boosts", {})
