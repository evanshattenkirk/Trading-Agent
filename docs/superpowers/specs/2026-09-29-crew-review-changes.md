# Crew review changes (approved 2026-09-29)

Source: the crew review doc (https://claude.ai/code/artifact/b3d5a435-9886-4ca8-b911-d289ae769e89). Evan approved
recommendations 1-9 and 11 ("the rest looks good, implement it all") and declined 10: the crew's auto-tweaks and
size-ups keep applying during the paper test.

## What changes

1. **Crew effects are logged.** A new `crew_log` journal table (session, ts, kind, book, detail JSON) holds:
   `directive` (each huddle's final votes, per-book multipliers and the roundtable's revisions), `block` (an entry a
   crew blackout or the VIX1D flag stopped, once per book and reason per day, with the desk that sourced it),
   `calendar_check` (weekly calendar vs Macro disagreements) and `usage` (the day's tokens and estimated cost, at
   the post-close huddle). `trades` gets a `crew` column: quantity at 1.0x next to the actual quantity, the size-up
   multiplier, the desks that cut, and (book A) the tweaks active at entry. `reporting/weekly_quant.py` adds a
   crew scorecard.
2. **Roundtable scope.** It can only revise the votes of desks in that huddle.
3. **Quant rules first.** Online, Haiku's reply adds notes; the rule-based vote, cooldown and pitches stay.
4. **Cost tally.** `cache_usage` counts output tokens, web searches and an estimated cost, in total and per desk.
5. **No 15:05 web call.** Vol is deterministic after the premarket huddle (item 7), so post-close costs nothing.
6. **Weekly event calendar.** Once a week (the first session of the week, at the 08:15 arrival) one web call saves
   the week's scheduled US events to `crew.calendar_path`. Each day its high-impact events become blackouts next to
   Macro's; blackouts are the union, since they only restrict. Each huddle compares the two and logs disagreements.
7. **Vol from Robinhood.** VIX from `get_index_quotes` (prior close as fallback); expected move from the
   engine's recorded 0DTE ATM straddle during the session, else from VIX. Vote 0.75 when VIX > 28. Robinhood has no
   VIX1D (checked 2026-09-29: `get_indexes` returns only VIX), so the premarket Vol brief still makes one web call
   for `vix1d_flag`, bias and the size-up vote; later Vol reads keep that vote unless VIX > 28.
8. **Fed and Rates fold into Macro.** Macro's call also covers the Fed and Treasuries and returns `fed` and `rates`
   sub-briefs. Fed and Rates stay in the office with derived briefs, fixed 1.0 votes, no events and no pitches
   (`INFO_ONLY`). Size-up voters are Macro and Vol; "Fed not hawkish" reads Macro's `fed.bias`.
9. **Votes by book.** `crew.vote_books` routes each desk's vote to the books its topic affects (default: Macro to
   A and C; Vol to A, B, C, D, G; Quant and Ops to A; nothing reaches E or F). The risk manager keeps one multiplier
   per book; A's is the old `size_mult`. Every LLM desk's prompt now describes all books and names the ones its vote
   reaches.
11. **Model.** `crew.model: claude-sonnet-5-5`; Haiku 4.5 stays for the roundtable and the daily Quant notes. Evan added a
    trial (2026-09-29): the weekly Quant report routine's analysis runs on Opus 5.5 at medium effort, with cheaper
    helpers (the Mac run on Sonnet 5.5, bulk reading on Haiku/Sonnet agents). It starts 2026-09-29, the first run is
    Friday 2026-10-02, and it is reviewed after that run. The change lives in the routine, not in this repo (the
    report script calls no model).

## Web calls per day

Before: 10 (4 premarket, 3 midday, 2 late, 1 post-close). After: Macro premarket and midday, Vol premarket, plus
the calendar once a week: about 3.

## Not changed

Size and loss limits, the size-up checklist (minus Rates), proposal whitelist, restrict-only desks, strategy rules.
