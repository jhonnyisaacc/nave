"""Telegram MarkdownV2 formatters for momentum scan payloads."""

from __future__ import annotations

import re
from typing import Any

_MD_V2_SPECIALS_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!])")


def escape_markdown_v2(value: Any) -> str:
    """Escape text content for Telegram MarkdownV2."""
    if value is None:
        return ""
    return _MD_V2_SPECIALS_RE.sub(r"\\\1", str(value))


def render_momentum_scan_markdown_v2(
    payload: dict[str, Any],
    *,
    max_message_chars: int = 3800,
) -> list[str]:
    """Render momentum scan output as Telegram-ready MarkdownV2 chunks."""
    if max_message_chars < 500:
        raise ValueError("max_message_chars must be at least 500")

    raw_summary = payload.get("summary")
    summary: dict[str, Any] = raw_summary if isinstance(raw_summary, dict) else {}
    raw_cadence = payload.get("cadence")
    cadence: dict[str, Any] = raw_cadence if isinstance(raw_cadence, dict) else {}
    raw_results = payload.get("results")
    results: dict[str, Any] = raw_results if isinstance(raw_results, dict) else {}

    strategy = escape_markdown_v2(payload.get("strategy") or "derivatives_momentum_v1")
    generated = escape_markdown_v2(
        str(payload.get("generated_at") or "").replace("T", " ").replace("+00:00", " UTC")
    )
    threshold = _safe_int(summary.get("effective_score_threshold") or summary.get("score_threshold"))
    risk_default = escape_markdown_v2(_extract_default_risk(results) or "0.5%")
    tradeable_count = _safe_int(summary.get("tradeable_count"))
    cadence_state = escape_markdown_v2(cadence.get("state") or summary.get("cadence_state") or "normal")

    blocks: list[str] = []
    header_lines = [
        "*NAVE Crypto*",
        f"Escaneo: {generated}",
        f"Estrategia: *{strategy}* \\| Umbral: *{threshold}* \\| Riesgo: *{risk_default}*",
        (
            f"Estado: *{cadence_state}* \\| Trade ahora: *{tradeable_count}* \\| "
            f"Confirmados: *{_count_confirmed(results)}*"
        ),
    ]
    note = cadence.get("note")
    if note:
        header_lines.append(f"Nota: {escape_markdown_v2(note)}")
    blocks.append("\n".join(header_lines))

    watch_rows = _build_watch_rows(results)
    if watch_rows:
        lines = ["*Watchlist prioritaria*"]
        for row in watch_rows:
            lines.append(
                (
                    f"\\- *{escape_markdown_v2(row['symbol'])}* \\| "
                    f"{escape_markdown_v2(row['action'])} \\| score *{row['score']}* \\| "
                    f"zona {escape_markdown_v2(_fmt_zone(row['entry_zone']))} \\| "
                    f"entrada ref {escape_markdown_v2(_fmt_price(row['entry_reference']))} \\| "
                    f"inv {escape_markdown_v2(_fmt_price(row['invalidation']))} \\| "
                    f"RR {escape_markdown_v2(_fmt_rr(row['rr_estimated']))}"
                )
            )
        blocks.append("\n".join(lines))

    for symbol in sorted(results.keys()):
        entry = results.get(symbol)
        if not isinstance(entry, dict):
            continue
        plans = entry.get("plans")
        if not isinstance(plans, list):
            continue

        best = _best_plan(plans)
        if not best:
            continue

        symbol_lines = [f"*{escape_markdown_v2(symbol)}*"]
        symbol_lines.append(
            f"Sesgo: {escape_markdown_v2(_side_label(best.get('side')))} \\| estado: {escape_markdown_v2(best.get('setup_status') or '?')}"
        )
        if _has_active_levels(best):
            symbol_lines.append(
                (
                    f"Zona: {escape_markdown_v2(_fmt_zone(best.get('entry_zone')))} \\| "
                    f"Entrada ref: {escape_markdown_v2(_fmt_price(_entry_reference(best)))} \\| "
                    f"Invalida: {escape_markdown_v2(_fmt_price(best.get('invalidation')))}"
                )
            )
            symbol_lines.append(
                (
                    f"TP1/TP2/TP3: {escape_markdown_v2(_fmt_price(best.get('tp1')))} / "
                    f"{escape_markdown_v2(_fmt_price(best.get('tp2')))} / "
                    f"{escape_markdown_v2(_fmt_price(best.get('tp3')))}"
                )
            )
        else:
            symbol_lines.append("Niveles: inactivos hasta nuevo breakout/retest valido")
        symbol_lines.append(
            f"RR: {escape_markdown_v2(_fmt_rr(best.get('rr_estimated')))} \\| score: *{_safe_int(best.get('confidence_score'))}*"
        )

        raw_diagnostics = best.get("diagnostics")
        diagnostics: dict[str, Any] = raw_diagnostics if isinstance(raw_diagnostics, dict) else {}
        if diagnostics.get("breakout_status") == "extended":
            symbol_lines.append("Estado: movimiento extendido; no trail de entrada fresca")
        funding = diagnostics.get("funding_rate")
        oi_change = diagnostics.get("oi_change_pct")
        if funding is not None or oi_change is not None:
            symbol_lines.append(
                (
                    f"Funding/OI: {escape_markdown_v2(_fmt_optional_float(funding, precision=8))} / "
                    f"{escape_markdown_v2(_fmt_optional_pct(oi_change))}"
                )
            )

        raw_reasons = best.get("reasoning")
        reasons: dict[str, Any] = raw_reasons if isinstance(raw_reasons, dict) else {}
        blockers = reasons.get("blockers") if isinstance(reasons.get("blockers"), list) else []
        confirms = reasons.get("confirmations") if isinstance(reasons.get("confirmations"), list) else []
        if blockers:
            symbol_lines.append(f"Falta: {escape_markdown_v2(_join_reason_lines(blockers, limit=2))}")
        elif confirms:
            symbol_lines.append(f"Confirma: {escape_markdown_v2(_join_reason_lines(confirms, limit=2))}")

        alt_plan = _best_plan(
            [plan for plan in plans if isinstance(plan, dict) and plan.get("side") != best.get("side")]
        )
        if alt_plan:
            symbol_lines.append(
                (
                    f"Lado opuesto: {escape_markdown_v2(_side_label(alt_plan.get('side')))} "
                    f"score {escape_markdown_v2(str(_safe_int(alt_plan.get('confidence_score'))))}"
                )
            )
        blocks.append("\n".join(symbol_lines))

    conclusion = (
        "*Conclusion:* "
        + (
            "hay setups tradeables ahora; validar trigger y riesgo antes de ejecutar\\."
            if tradeable_count > 0
            else "sin trade ahora\\. Esperar validacion 4H/trigger 1H; no anticipar\\."
        )
    )
    blocks.append(conclusion)

    fragments: list[str] = []
    for block in blocks:
        fragments.extend(_split_block_lines(block, max_chars=max_message_chars))

    messages = _pack_fragments(fragments, max_chars=max_message_chars)
    if len(messages) <= 1:
        return messages

    total = len(messages)
    return [f"*Parte {idx}/{total}*\n{message}" for idx, message in enumerate(messages, start=1)]


def render_entry_zone_alert_markdown_v2(event: dict[str, Any]) -> str:
    """Render an entry-zone touch event for Telegram MarkdownV2."""
    alert_kind = str(event.get("alert_kind") or "entry_zone")
    symbol = escape_markdown_v2(event.get("symbol") or "?")
    side = escape_markdown_v2(_side_label(event.get("side")))
    price_now = escape_markdown_v2(_fmt_price(event.get("price")))
    entry_zone = escape_markdown_v2(_fmt_zone(event.get("entry_zone")))
    invalidation = escape_markdown_v2(_fmt_price(event.get("invalidation")))
    score = escape_markdown_v2(str(_safe_int(event.get("confidence_score"))))
    rr = escape_markdown_v2(_fmt_rr(event.get("rr_estimated")))
    expected_move = escape_markdown_v2(_fmt_optional_pct(event.get("expected_move_pct")))
    targets = escape_markdown_v2(
        " / ".join(
            [
                _fmt_price(event.get("tp1")),
                _fmt_price(event.get("tp2")),
                _fmt_price(event.get("tp3")),
            ]
        )
    )

    if alert_kind == "breakdown_watch":
        title = "*BREAKDOWN WATCH*"
        context = f"{symbol} rompio 4H con sesgo {side}; retest/trigger 1H pendiente"
        action = "Accion: no perseguir market; esperar retest/trigger o evaluar hedge chico\\."
    else:
        title = "*Alerta de Entrada*"
        context = f"{symbol} entro en zona \\({side}\\)"
        action = "Accion: esperar confirmacion de trigger antes de ejecutar\\."

    return "\n".join(
        [
            title,
            context,
            f"Precio actual: *{price_now}*",
            f"Zona: {entry_zone}",
            f"Invalidacion: {invalidation} \\| score: *{score}* \\| RR: {rr}",
            f"TP1/TP2/TP3: {targets} \\| move esp: {expected_move}",
            action,
        ]
    )


def render_universe_momentum_scan(payload: dict[str, Any], *, max_rows: int = 20) -> str:
    """Render a human-readable research replay without action language."""
    lines = [
        "Crypto universe momentum discovery (research only)",
        f"Window: {payload.get('window', {}).get('start', '—')} -> {payload.get('window', {}).get('end', '—')}",
        f"Hypothesis: {payload.get('hypothesis', '—')}",
        "",
    ]
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    coverage = metrics.get("candidate_coverage") if isinstance(metrics.get("candidate_coverage"), dict) else {}
    precision = metrics.get("precision") if isinstance(metrics.get("precision"), dict) else {}
    lines.extend(
        [
            f"Eligible detections: {coverage.get('eligible_detections', 0)}",
            f"Outcome coverage: {coverage.get('outcome_coverage', 0):.1%}",
            f"Meaningful-move precision: {precision.get('meaningful_move_precision', 0):.1%}",
            "",
            "Latest ranked observations:",
        ]
    )
    observations = payload.get("observations") if isinstance(payload.get("observations"), list) else []
    latest = observations[-1] if observations else {}
    rows = latest.get("top_candidates") or latest.get("candidates") or []
    for candidate in rows[:max_rows]:
        if not isinstance(candidate, dict):
            continue
        lines.append(
            " · ".join(
                [
                    str(candidate.get("symbol") or "?"),
                    f"score={candidate.get('rank_score') if candidate.get('rank_score') is not None else '—'}",
                    str(candidate.get("ranking_state") or "UNKNOWN"),
                    f"liq={candidate.get('liquidity', {}).get('state', 'UNKNOWN')}",
                ]
            )
        )
    lines.extend(["", "Target audit:"])
    for target in payload.get("target_report", [])[:max_rows]:
        if isinstance(target, dict):
            lines.append(f"{target.get('target', '?')}: {target.get('status', 'UNKNOWN')}")
    return "\n".join(lines)


def _count_confirmed(results: dict[str, Any]) -> int:
    count = 0
    for entry in results.values():
        if not isinstance(entry, dict):
            continue
        plans = entry.get("plans")
        if not isinstance(plans, list):
            continue
        count += sum(1 for plan in plans if isinstance(plan, dict) and plan.get("setup_status") == "confirmed")
    return count


def _build_watch_rows(results: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for symbol, entry in results.items():
        if not isinstance(entry, dict):
            continue
        plans = entry.get("plans")
        if not isinstance(plans, list):
            continue
        best = _best_plan(plans)
        if not best or not _has_active_levels(best):
            continue
        rows.append(
            {
                "symbol": symbol,
                "action": "TRADEABLE" if bool(best.get("tradeable")) else "WATCH",
                "score": _safe_int(best.get("confidence_score")),
                "entry_zone": best.get("entry_zone"),
                "entry_reference": _entry_reference(best),
                "invalidation": best.get("invalidation"),
                "rr_estimated": best.get("rr_estimated"),
            }
        )
    rows.sort(key=lambda row: (-int(row["score"]), str(row["symbol"])))
    return rows


def _best_plan(plans: list[Any]) -> dict[str, Any] | None:
    valid = [plan for plan in plans if isinstance(plan, dict)]
    if not valid:
        return None
    return sorted(
        valid,
        key=lambda plan: (
            bool(plan.get("tradeable")),
            _status_rank(plan.get("setup_status")),
            _safe_int(plan.get("confidence_score")),
        ),
        reverse=True,
    )[0]


def _status_rank(status: Any) -> int:
    status_text = str(status or "").lower()
    if status_text == "confirmed":
        return 2
    if status_text == "pending":
        return 1
    return 0


def _has_active_levels(plan: dict[str, Any]) -> bool:
    return bool(plan.get("tradeable")) or str(plan.get("setup_status") or "").lower() in {
        "confirmed",
        "pending",
    }


def _entry_reference(plan: dict[str, Any]) -> float | None:
    zone = plan.get("entry_zone")
    if not isinstance(zone, list) or not zone:
        return None
    try:
        if str(plan.get("side") or "").lower() == "short":
            return float(zone[0])
        return float(zone[-1])
    except (TypeError, ValueError):
        return None


def _extract_default_risk(results: dict[str, Any]) -> str | None:
    for entry in results.values():
        if not isinstance(entry, dict):
            continue
        plans = entry.get("plans")
        if not isinstance(plans, list):
            continue
        for plan in plans:
            if not isinstance(plan, dict):
                continue
            sizing = plan.get("sizing")
            if not isinstance(sizing, dict):
                continue
            raw_risk = sizing.get("risk_pct")
            if raw_risk is None:
                continue
            try:
                return f"{float(raw_risk) * 100:.2f}%"
            except (TypeError, ValueError):
                continue
    return None


def _fmt_zone(zone: Any) -> str:
    if isinstance(zone, list) and len(zone) >= 2:
        return f"{_fmt_price(zone[0])} - {_fmt_price(zone[1])}"
    return "N/A"


def _fmt_price(value: Any) -> str:
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_rr(value: Any) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_optional_float(value: Any, *, precision: int) -> str:
    try:
        return f"{float(value):.{precision}f}"
    except (TypeError, ValueError):
        return "N/A"


def _fmt_optional_pct(value: Any) -> str:
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return "N/A"


def _side_label(side: Any) -> str:
    side_text = str(side or "?").lower()
    if side_text == "long":
        return "LONG"
    if side_text == "short":
        return "SHORT"
    return side_text.upper() if side_text else "?"


def _join_reason_lines(values: list[Any], *, limit: int) -> str:
    text_values = [str(value) for value in values if str(value).strip()]
    if not text_values:
        return ""
    if len(text_values) <= limit:
        return " | ".join(text_values)
    remaining = len(text_values) - limit
    return " | ".join(text_values[:limit]) + f" (+{remaining} mas)"


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _split_block_lines(block: str, *, max_chars: int) -> list[str]:
    if len(block) <= max_chars:
        return [block]

    out: list[str] = []
    current = ""
    for line in block.splitlines():
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            out.append(current)
        if len(line) <= max_chars:
            current = line
            continue
        start = 0
        while start < len(line):
            out.append(line[start:start + max_chars])
            start += max_chars
        current = ""

    if current:
        out.append(current)
    return out


def _pack_fragments(fragments: list[str], *, max_chars: int) -> list[str]:
    out: list[str] = []
    current = ""
    for fragment in fragments:
        candidate = fragment if not current else f"{current}\n\n{fragment}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            out.append(current)
        current = fragment
    if current:
        out.append(current)
    return out


__all__ = [
    "escape_markdown_v2",
    "render_entry_zone_alert_markdown_v2",
    "render_momentum_scan_markdown_v2",
]
