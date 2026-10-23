import csv
import html
import json
import os
import shutil
import threading
import time
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BET_LOG_CSV = os.path.join(SCRIPT_DIR, "placed_bets_log.csv")
UNSAVED_JSONL = os.path.join(SCRIPT_DIR, "placed_bets_unsaved.jsonl")
REPORT_HTML = os.path.join(SCRIPT_DIR, "bets_report.html")
csv_lock = threading.Lock()

CSV_COLUMNS = [
    "Date",
    "Game",
    "Sport",
    "League",
    "Market Type",
    "Outcome",
    "Pinnacle Point",
    "Betway Line",
    "Bookie Odds",
    "Pinnacle Odds",
    "NVP",
    "EV %",
    "Stake",
    "Status",
    "Return",
    "Profit",
    "Final Score",
    "Booking Code",
    "Betslip ID",
    "Betway Event ID",
    "Pinnacle Event ID",
]

SETTLED_STATUSES = ("Win", "Loss", "Void", "Cashed Out")
TOTAL_MARKER = "TOTAL"  # first cell of the summary row that sits at the bottom


def _to_float(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


# ============================================================
# SUMMARY (the TOTAL row kept at the bottom of the CSV)
# ============================================================


def _build_summary(rows):
    counts = {s: 0 for s in SETTLED_STATUSES}
    pending = 0
    staked = returned = profit = gross_win = gross_loss = 0.0
    for r in rows:
        st = (r.get("Status") or "").strip()
        if st in counts:
            counts[st] += 1
            staked += _to_float(r.get("Stake")) or 0.0  # settled only (for ROI)
            returned += _to_float(r.get("Return")) or 0.0
            p = _to_float(r.get("Profit")) or 0.0
            profit += p
            if p > 0:
                gross_win += p
            elif p < 0:
                gross_loss += -p
        elif st.lower() == "pending":
            pending += 1
    settled = sum(counts.values())
    roi = (profit / staked * 100.0) if staked else 0.0
    return {
        "settled": settled,
        "pending": pending,
        "counts": counts,
        "staked": staked,
        "returned": returned,
        "profit": profit,
        "gross_win": gross_win,
        "gross_loss": gross_loss,
        "roi": roi,
        "total": len(rows),
    }


def _summary_row(rows):
    s = _build_summary(rows)
    c = s["counts"]
    return {
        "Date": TOTAL_MARKER,
        "Game": (
            f"{s['total']} bets | settled {s['settled']} "
            f"(W {c['Win']} / L {c['Loss']} / V {c['Void']} / CO {c['Cashed Out']}) "
            f"| pending {s['pending']} | staked {s['staked']:,.2f} "
            f"| net {s['profit']:+,.2f} | ROI {s['roi']:+.1f}%"
        ),
        "Stake": round(s["staked"], 2),
        "Status": TOTAL_MARKER,
        "Return": round(s["returned"], 2),
        "Profit": round(s["profit"], 2),
        "Final Score": f"Won {s['gross_win']:,.2f} / Lost {s['gross_loss']:,.2f}",
    }


def _print_summary(rows):
    s = _build_summary(rows)
    c = s["counts"]
    print(
        f"[settle] 📊 settled {s['settled']} "
        f"(W {c['Win']} / L {c['Loss']} / V {c['Void']} / CO {c['Cashed Out']}) "
        f"| pending {s['pending']} | staked ₦{s['staked']:,.2f} "
        f"| net ₦{s['profit']:+,.2f}"
    )


# ============================================================
# STYLED REPORT (bets_report.html) — rebuilt whenever the CSV is written
# ============================================================

_REPORT_COLS = [
    ("Date", "Date"),
    ("Game", "Game"),
    ("League", "League"),
    ("Market Type", "Market"),
    ("Outcome", "Outcome"),
    ("Bookie Odds", "Odds"),
    ("EV %", "EV %"),
    ("Stake", "Stake"),
    ("Status", "Status"),
    ("Return", "Return"),
    ("Profit", "Profit"),
    ("Final Score", "Score"),
    ("Booking Code", "Code"),
]


def _write_report(rows):
    """Writes a colour-coded HTML view of the log with big bold totals. Never raises."""
    try:
        s = _build_summary(rows)
        c = s["counts"]
        net_cls = "pos" if s["profit"] > 0 else ("neg" if s["profit"] < 0 else "")
        esc = html.escape

        head = "".join(f"<th>{esc(label)}</th>" for _, label in _REPORT_COLS)
        body = []
        for r in rows:
            st = (r.get("Status") or "").strip()
            cls = {
                "Win": "win",
                "Loss": "loss",
                "Void": "void",
                "Cashed Out": "void",
            }.get(st, "pend")
            tds = "".join(
                f"<td>{esc(str(r.get(k, '') or ''))}</td>" for k, _ in _REPORT_COLS
            )
            body.append(f'<tr class="{cls}">{tds}</tr>')

        n = len(_REPORT_COLS)
        totals = (
            f'<tr class="total"><td colspan="{n}">'
            f'<div class="tline">'
            f"<span>TOTAL BETS <b>{s['total']}</b></span>"
            f"<span>SETTLED <b>{s['settled']}</b> "
            f"(W {c['Win']} / L {c['Loss']} / V {c['Void']} / CO {c['Cashed Out']})</span>"
            f"<span>PENDING <b>{s['pending']}</b></span>"
            f"<span>STAKED <b>₦{s['staked']:,.2f}</b></span>"
            f"<span>RETURNED <b>₦{s['returned']:,.2f}</b></span>"
            f'<span>WON <b class="pos">₦{s["gross_win"]:,.2f}</b></span>'
            f'<span>LOST <b class="neg">₦{s["gross_loss"]:,.2f}</b></span>'
            f'<span>NET <b class="{net_cls}">₦{s["profit"]:+,.2f}</b></span>'
            f'<span>ROI <b class="{net_cls}">{s["roi"]:+.1f}%</b></span>'
            f"</div></td></tr>"
        )

        page = f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="15">
<title>Bets report</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#f4f5f7;color:#1f2937}}
.wrap{{padding:12px 12px 130px}}
table{{border-collapse:collapse;width:100%;font-size:13px;background:#fff}}
th{{background:#e5e7eb;text-align:left;padding:6px 8px;position:sticky;top:0}}
td{{padding:5px 8px;border-bottom:1px solid #eee;white-space:nowrap}}
tr.win td{{background:#ecfdf5}} tr.loss td{{background:#fef2f2}}
tr.void td{{background:#fffbeb}} tr.pend td{{color:#6b7280}}
.total{{position:fixed;left:0;right:0;bottom:0}}
.total td{{background:#0f172a;color:#fff;border:0;padding:0}}
.tline{{display:flex;flex-wrap:wrap;gap:10px 28px;padding:14px 18px;font-size:20px;font-weight:800;letter-spacing:.3px}}
.tline b{{font-size:26px;color:#fde047}}
.tline b.pos{{color:#4ade80}} .tline b.neg{{color:#f87171}}
</style></head><body><div class="wrap">
<table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody>
<tfoot>{totals}</tfoot></table></div></body></html>"""

        tmp = REPORT_HTML + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(page)
        os.replace(tmp, REPORT_HTML)
    except Exception as e:
        print(f"[report] WARNING: could not update {REPORT_HTML} — {e}")


# ============================================================
# LOW-LEVEL FILE HELPERS (callers must hold csv_lock)
# ============================================================


def _read_rows():
    """Reads the bet rows. Ignores padding spaces, blank lines and the TOTAL row."""
    with open(BET_LOG_CSV, "r", newline="", encoding="utf-8-sig") as f:
        raw = list(csv.reader(f))
    if not raw:
        return [], []
    header = [h.strip() for h in raw[0]]
    rows = []
    for vals in raw[1:]:
        if not any(v.strip() for v in vals):
            continue
        if vals[0].strip().upper() == TOTAL_MARKER:
            continue
        rows.append(
            {header[i]: vals[i].strip() for i in range(min(len(header), len(vals)))}
        )
    return header, rows


def _write_rows(rows):
    """
    Writes all bet rows followed by the TOTAL row (always last).
    Goes via a temp file then swaps it in, so a crash can't leave a half-written log.
    """
    tmp = BET_LOG_CSV + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({c: r.get(c, "") for c in CSV_COLUMNS})
        if rows:
            writer.writerow(_summary_row(rows))
    os.replace(tmp, BET_LOG_CSV)
    _write_report(rows)


def _add_rows(new_rows):
    """Adds bets and rewrites the file so the TOTAL row stays at the bottom."""
    rows = []
    if os.path.exists(BET_LOG_CSV) and os.path.getsize(BET_LOG_CSV) > 0:
        _, rows = _read_rows()
    rows.extend(new_rows)
    _write_rows(rows)


def _flush_unsaved():
    """Moves rows that couldn't be saved earlier into the CSV. Hold csv_lock."""
    if not os.path.exists(UNSAVED_JSONL):
        return
    try:
        with open(UNSAVED_JSONL, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        if rows:
            _add_rows(rows)
        os.remove(UNSAVED_JSONL)
        if rows:
            print(f"[csv] recovered {len(rows)} previously unsaved row(s)")
    except OSError:
        pass  # CSV unavailable; try again next time


# ============================================================
# SCHEMA CHECK (runs once at startup)
# ============================================================


def ensure_csv_schema():
    """
    Upgrades the column layout if it differs (one backup file, overwritten),
    and refreshes the TOTAL row at the bottom. Existing rows are never lost.
    """
    with csv_lock:
        if not os.path.exists(BET_LOG_CSV) or os.path.getsize(BET_LOG_CSV) == 0:
            return
        try:
            header, rows = _read_rows()
        except OSError as e:
            print(f"[csv] WARNING: could not read {BET_LOG_CSV} — {e}")
            return

        backup = BET_LOG_CSV[: -len(".csv")] + "_backup.csv"
        try:
            if header != CSV_COLUMNS:
                shutil.copy2(BET_LOG_CSV, backup)
                print(
                    f"[csv] Column layout updated — {len(rows)} existing row(s) kept, "
                    f"backup saved as {backup}"
                )
            _write_rows(rows)  # also (re)creates the TOTAL row
        except OSError as e:
            print(f"[csv] WARNING: could not update {BET_LOG_CSV} — {e}")


# ============================================================
# ADD A PLACED BET
# ============================================================


def append_bet(result: dict, bet_result: dict):
    """One row per successfully placed bet. Status starts as Pending."""
    booking_code = ""
    betslip_id = ""
    try:
        inner = bet_result["response"]["betResponses"][0]
        booking_code = inner.get("bookingCode") or ""
        betslip_id = inner.get("betslipId") or ""
    except (KeyError, IndexError, TypeError):
        pass

    row = {
        "Date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Game": f"{result['home']} vs {result['away']}",
        "Sport": result.get("sport") or "",
        "League": result.get("league") or "",
        "Market Type": result.get("line_type") or "",
        "Outcome": result.get("outcome") or "",
        "Pinnacle Point": result.get("points")
        if result.get("points") not in (None, "")
        else "",
        "Betway Line": result.get("betway_matched_line")
        if result.get("betway_matched_line") is not None
        else "",
        "Bookie Odds": result.get("betway_price"),
        "Pinnacle Odds": result.get("pinnacle_price")
        if result.get("pinnacle_price") is not None
        else "",
        "NVP": result.get("nvp"),
        "EV %": result.get("ev_percent"),
        "Stake": bet_result.get("stake"),
        "Status": "Pending",
        "Return": "",
        "Profit": "",
        "Final Score": "",
        "Booking Code": booking_code,
        "Betslip ID": betslip_id,
        "Betway Event ID": result.get("betway_event_id")
        if result.get("betway_event_id") is not None
        else "",
        "Pinnacle Event ID": result.get("pinnacle_event_id")
        if result.get("pinnacle_event_id") is not None
        else "",
    }

    with csv_lock:
        try:
            _flush_unsaved()
            _add_rows([row])
        except OSError as e:
            try:
                with open(UNSAVED_JSONL, "a", encoding="utf-8") as f:
                    f.write(json.dumps(row) + "\n")
                print(
                    f"[csv] WARNING: could not write {BET_LOG_CSV} — {e}. "
                    f"Row kept in {UNSAVED_JSONL} and will be added automatically."
                )
            except OSError as e2:
                print(
                    f"[csv] ERROR: could not save the row anywhere — {e2}. Row: {row}"
                )


# ============================================================
# SETTLEMENT (Pending -> Win / Loss / Void / Cashed Out)
# ============================================================


def _status_from_betslip(b: dict, stake, ret):
    """
    Returns (status, mapped_from_money). Betway's own wording is used
    when recognised; otherwise the status is worked out from the money.
    """
    raw = (
        str(b.get("settlementStatus") or b.get("resultingStatus") or "").strip().lower()
    )

    if raw in ("win", "won"):
        return "Win", False
    if raw in ("loss", "lost", "lose"):
        return "Loss", False
    if "void" in raw or "cancel" in raw or "refund" in raw:
        return "Void", False
    if "cash" in raw:
        return "Cashed Out", False

    if ret is not None and stake is not None:
        if ret <= 0:
            return "Loss", True
        if abs(ret - stake) < 0.01:
            return "Void", True
        if ret > stake:
            return "Win", True
        return "Cashed Out", True  # 0 < ret < stake
    return None, False


def _event_ids(b):
    ids = set()
    for leg in b.get("bets") or []:
        if leg.get("eventId") is not None:
            ids.add(str(leg["eventId"]))
    return ids


def settle_pending(fetch_page, page_size=50, max_pages=6):
    """
    fetch_page(page_number, page_size) -> list of settled betslips from Betway.
    Matches each Pending row by booking code / betslip ID (rows with neither
    fall back to Betway event ID + stake when unique). Returns how many rows
    were settled this round.
    """
    # 1) What is pending right now?
    with csv_lock:
        _flush_unsaved()
        if not os.path.exists(BET_LOG_CSV) or os.path.getsize(BET_LOG_CSV) == 0:
            return 0
        try:
            _, rows = _read_rows()
        except OSError as e:
            print(f"[settle] WARNING: could not read {BET_LOG_CSV} — {e}")
            return 0

    pending = [r for r in rows if (r.get("Status") or "").strip().lower() == "pending"]
    if not pending:
        _print_summary(rows)
        return 0

    codes = {(r.get("Booking Code") or "").strip() for r in pending} - {""}
    ids = {str(r.get("Betslip ID") or "").strip() for r in pending} - {""}
    has_unidentified = any(
        not (r.get("Booking Code") or "").strip()
        and not str(r.get("Betslip ID") or "").strip()
        for r in pending
    )

    # 2) Fetch settled betslips (newest first)
    all_slips, seen = [], set()
    for page in range(max_pages):
        betslips = fetch_page(page, page_size)
        if not betslips:
            break

        new_on_page = 0
        for b in betslips:
            bid = str(b.get("betslipId"))
            if bid in seen:
                continue
            seen.add(bid)
            new_on_page += 1
            all_slips.append(b)

        if new_on_page == 0 or len(betslips) < page_size:
            break

        if not has_unidentified:
            got_codes = {(s.get("bookingCode") or "").strip() for s in all_slips}
            got_ids = {str(s.get("betslipId")) for s in all_slips}
            if codes <= got_codes and ids <= got_ids:
                break
        time.sleep(0.3)

    if not all_slips:
        print("[settle] Betway returned no settled bets")
        _print_summary(rows)
        return 0

    by_code = {
        (s.get("bookingCode") or "").strip(): s
        for s in all_slips
        if s.get("bookingCode")
    }
    by_id = {str(s.get("betslipId")): s for s in all_slips}
    used = set()

    # 3) Re-read the CSV (new bets may have been added meanwhile) and apply results
    settled = 0
    with csv_lock:
        try:
            _, rows = _read_rows()
        except OSError as e:
            print(f"[settle] WARNING: could not read {BET_LOG_CSV} — {e}")
            return 0

        icons = {"Win": "✅", "Loss": "❌", "Void": "➖", "Cashed Out": "💰"}

        for r in rows:
            if (r.get("Status") or "").strip().lower() != "pending":
                continue
            code = (r.get("Booking Code") or "").strip()
            bid = str(r.get("Betslip ID") or "").strip()
            b = (by_code.get(code) if code else None) or (
                by_id.get(bid) if bid else None
            )

            # Old rows with neither code nor ID: Betway event ID + stake, only if unique
            if b is None and not code and not bid:
                ev = str(r.get("Betway Event ID") or "").strip()
                row_stake = _to_float(r.get("Stake"))
                if ev and row_stake is not None:
                    cands = []
                    for s in all_slips:
                        if str(s.get("betslipId")) in used:
                            continue
                        w = _to_float(s.get("wageredAmount"))
                        if (
                            w is not None
                            and abs(w - row_stake) < 0.01
                            and ev in _event_ids(s)
                        ):
                            cands.append(s)
                    if len(cands) == 1:
                        b = cands[0]
            if b is None:
                continue

            stake = _to_float(b.get("wageredAmount"))
            ret = _to_float(b.get("actualReturnAmount"))
            status, _ = _status_from_betslip(b, stake, ret)
            if status is None:
                continue

            profit = (ret - stake) if (ret is not None and stake is not None) else None
            bets = b.get("bets") or []

            r["Status"] = status
            r["Return"] = round(ret, 2) if ret is not None else ""
            r["Profit"] = round(profit, 2) if profit is not None else ""
            r["Final Score"] = (bets[0].get("finalScore") or "") if bets else ""
            if not bid:
                r["Betslip ID"] = b.get("betslipId") or ""
            if not code:
                r["Booking Code"] = b.get("bookingCode") or ""
            used.add(str(b.get("betslipId")))
            settled += 1

            pl = f"₦{profit:+,.2f}" if profit is not None else ""
            print(
                f"{icons.get(status, 'ℹ️')} {status} {pl} — {r.get('Game')} "
                f"[{r.get('Market Type')} {r.get('Outcome')}]"
            )

        if settled:
            try:
                _write_rows(rows)
            except OSError as e:
                print(
                    f"[settle] WARNING: could not save {BET_LOG_CSV} — {e} (will retry)"
                )
                return 0

    _print_summary(rows)
    return settled
