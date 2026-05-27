"""Generate PDF listing all modules built so far (testing, trading, strategies)."""
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
)
from reportlab.lib.enums import TA_LEFT


SECTIONS = [
    ("Engine / Core", [
        ("replay_engine.py",
         "ReplayEngine (streams tick CSVs), SignalDetector (CB ±N detection), "
         "BookState, TradeEvent, replay_with_trades (interleaves ticks + trades)"),
        ("order_engine.py",
         "PaperOrderEngine (150ms latency + stale-ask + min-notional + trade-event "
         "maker queue), LiveOrderEngine (wraps py_clob_client_v2, queries get_trades "
         "for true fill price), Order, Fill, Side, OrderType"),
        ("strategies.py",
         "Strategy abstract base, S1AlignedHoldStrategy, S2FadeStrategy, "
         "S6MomentumStrategy, S7FadeStrategy (production), CombinedS2S6Strategy, "
         "StrategyRunner, dynamic_size helper (Option B sizing)"),
    ]),
    ("Live Trading Bot (production)", [
        ("btc_s7_fade.py",
         "Live S7 bot: fades $18+ CB moves with $0.10-$0.60 entry window, "
         "Option B dynamic shares, real-resolution exit pricing, full timing "
         "instrumentation. Running on EC2 in tmux session btcS7."),
    ]),
    ("Validators / Backtesters", [
        ("s7_full_validation.py",
         "Full S7 backtest across all tick windows. Reports Wilson CI on WR, "
         "share size distribution, $/day projection."),
        ("s7_signal_level_wr.py",
         "Multi-shot signal-level WR (every CB move counted) — measures the "
         "underlying edge separately from single-shot strategy WR."),
        ("s7_validate_real_resolutions.py",
         "Compares strategy WR using real Polymarket outcomes vs final-bid heuristic. "
         "Confirms heuristic correctness."),
        ("s7_wr_by_price_bucket.py",
         "Conditional WR by fade entry bucket — reveals non-uniform WR pattern: "
         "cheap fades 29% WR, mid 91%, expensive 88%."),
        ("s7_capped_entry.py",
         "Cap entry analysis: compares no-cap vs $0.45 vs $0.60. Found $0.60 "
         "is the sweet spot (same actual P&L, 2x worst-case EV)."),
        ("cheap_fade_floor.py",
         "Death-zone analysis: bucketed WR for cheap fades. Identified $0.05-$0.10 "
         "as 0/5 death zone."),
        ("validate_phase35.py",
         "Taker vs maker mode comparison using trade events. Confirms maker mode "
         "doesn't fill in 5-min markets."),
        ("validate_s7_framework.py",
         "Sanity check: S7FadeStrategy through StrategyRunner reproduces "
         "single-shot behavior matching live bot."),
        ("find_edge.py",
         "S2 threshold sweep across thresholds $15-35. Used to discover $18 "
         "is the optimal threshold."),
        ("compare_min_notional_fix.py",
         "A/B/C comparison: no min-notional vs min-notional-only vs Option B fix. "
         "Confirms Option B rescues 4 of 37 historical signals with no degradation."),
        ("latency_sensitivity.py",
         "Sweep S7 backtest at multiple taker_latency_ms values (150/500/1000/2500/5000ms) "
         "to test how sensitive WR/$/day is to latency modeling."),
    ]),
    ("Risk Analysis / Projections", [
        ("bust_probability.py / bust_v2.py",
         "Monte Carlo wallet trajectory simulation. Reports P(bust) across the "
         "95% CI range to inform risk management."),
        ("s7_projections.py",
         "Per-trade EV table by fade entry × WR. Daily/weekly/monthly $ projections "
         "with corrected fee formula."),
        ("fine_ev_table.py",
         "Fine-grained EV per fade entry (every $0.05 step). Identifies break-even "
         "fade entry at each WR scenario."),
    ]),
    ("Smoke Tests", [
        ("smoke_trade_sim.py",
         "Trade-event simulator sanity test. Verifies replay_with_trades interleaves "
         "correctly and on_trade advances maker queue."),
        ("live_smoke_test.py",
         "LiveOrderEngine end-to-end test. Places a $0.01 maker (zero risk), "
         "verifies it appears in get_open_orders, cancels it. Used pre-deploy."),
    ]),
    ("Data / Infrastructure", [
        ("market_resolutions.py",
         "Cached real Polymarket outcomes (gamma-api closed=true) for all 361 "
         "tick windows. Reusable for any future strategy validator."),
        ("tick_recorder.py",
         "Captures tick + trade-event CSVs to /home/ubuntu/reports/. Running "
         "24/7 in tmux session tickRecorderBTC."),
        ("check_balance.py",
         "Wallet USDC balance check via py_clob_client_v2 BalanceAllowanceParams."),
    ]),
]


def build_pdf(out_path):
    doc = SimpleDocTemplate(out_path, pagesize=letter,
                            leftMargin=0.5 * inch, rightMargin=0.5 * inch,
                            topMargin=0.6 * inch, bottomMargin=0.6 * inch)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("title", parent=styles["Title"],
                                  fontSize=18, spaceAfter=12)
    subtitle_style = ParagraphStyle("subtitle", parent=styles["Normal"],
                                     fontSize=10, textColor=colors.grey,
                                     spaceAfter=20)
    section_style = ParagraphStyle("section", parent=styles["Heading2"],
                                    fontSize=14, textColor=colors.HexColor("#1f4e79"),
                                    spaceBefore=14, spaceAfter=6)
    cell_style = ParagraphStyle("cell", parent=styles["Normal"],
                                 fontSize=8.5, leading=10.5)
    file_style = ParagraphStyle("file", parent=cell_style,
                                 fontName="Courier-Bold", fontSize=8.5)

    story = []
    story.append(Paragraph("Polymarket S7 — Modules Inventory", title_style))
    story.append(Paragraph(
        "All testing, trading, and strategy modules built through 2026-05-26. "
        "Repository: github.com/SeanGoldsborough/polymarket-reversal-sniper",
        subtitle_style))

    for section_name, rows in SECTIONS:
        story.append(Paragraph(section_name, section_style))
        table_data = [["File", "Purpose"]]
        for fname, purpose in rows:
            table_data.append([
                Paragraph(fname, file_style),
                Paragraph(purpose, cell_style),
            ])
        tbl = Table(table_data, colWidths=[2.0 * inch, 5.0 * inch])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f4e79")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), 10),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
            ("TOPPADDING", (0, 0), (-1, 0), 4),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.lightgrey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f5f5f5")]),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 6))

    doc.build(story)


if __name__ == "__main__":
    out = "/tmp/sniper-fresh/S7_modules_inventory.pdf"
    build_pdf(out)
    print(f"Wrote {out}")
