"""Strength math: estimated 1RM, intensity, load and volume. No database access."""

from __future__ import annotations

from datetime import date


def estimate_1rm(weight: float, reps: int, rir: int | None) -> float | None:
    """Estimated one-rep max from a set. Only sets with RIR below 6 qualify."""
    if rir is None or rir >= 6:
        return None
    divisor = 30 if rir <= 3 else 40
    return weight * (1 + (reps + rir) / divisor)


def compute_strength(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Add 1RM, intensity, load and volume to each set, plus a per-day summary.

    rows must be ordered by date, then lift, set position, subset position.
      e1rm       estimated 1RM from this set (RIR < 6 only)
      daily_1rm  best e1rm that day
      intensity  weight / daily_1rm
      load       intensity * weight * reps / (1 + 0.5 * rir)
      volume     running total of load within the day
    Days where no set qualifies for a 1RM estimate get empty
    intensity, load and volume instead of an error.
    """
    sets = []
    for r in rows:
        weight, reps, rir = float(r["weight"]), int(r["reps"]), r["rir"]
        sets.append({
            "date": r["date"],
            "lift_id": r["lift_id"],
            "set_position": r["set_position"],
            "subset_position": r["subset_position"],
            "weight": weight,
            "reps": reps,
            "rir": rir,
            "e1rm": estimate_1rm(weight, reps, rir),
        })

    best: dict[date, float] = {}
    for s in sets:
        if s["e1rm"] is not None:
            best[s["date"]] = max(best.get(s["date"], 0.0), s["e1rm"])

    running: dict[date, float] = {}
    for s in sets:
        daily = best.get(s["date"])
        s["daily_1rm"] = daily
        if daily and s["rir"] is not None:
            s["intensity"] = s["weight"] / daily
            s["load"] = s["intensity"] * s["weight"] * s["reps"] / (1 + 0.5 * s["rir"])
            running[s["date"]] = running.get(s["date"], 0.0) + s["load"]
            s["volume"] = running[s["date"]]
        else:
            s["intensity"] = s["load"] = None
            s["volume"] = running.get(s["date"])

    days: dict[date, dict] = {}
    for s in sets:
        d = days.setdefault(s["date"], {
            "date": s["date"], "daily_1rm": s["daily_1rm"],
            "top_weight": 0.0, "set_count": 0, "volume": None,
        })
        d["top_weight"] = max(d["top_weight"], s["weight"])
        d["set_count"] += 1
        if s["volume"] is not None:
            d["volume"] = s["volume"]

    def rounded(item: dict) -> dict:
        return {k: round(v, 2) if isinstance(v, float) else v for k, v in item.items()}

    return [rounded(s) for s in sets], [rounded(d) for d in days.values()]
