"""MCP prompts: reusable instructions a client can offer as one-click actions."""

from __future__ import annotations

from .stats import parse_period

MONTHLY_FROM_DAYS = 120  # longer periods read better as monthly buckets


def retrospective_prompt(period: str = "90d") -> str:
    """Instructions for a visual retrospective built from the `activity` tool."""
    days = parse_period(period)  # validates; raises ValueError on bad input
    group_by = "month" if days > MONTHLY_FROM_DAYS else "week"
    unit = group_by
    return f"""\
Make me a visual retrospective of what has occupied my time, using my Second Brain.

PERIOD: last {period}, by {unit}.

1. Get the data
- Call the `activity` tool with period="{period}", group_by="{group_by}", focus="all".
- Use ONLY the numbers it returns. Never invent or estimate values. "Score" is weighted activity
  (writes and conversations count more than reads), not hours, so never call it time spent.
- If totals.events is under 30, still build the report, but show a visible "early days, small sample"
  note and keep the conclusions modest.
- For context on the top 3 topics or projects, you may call `read` or `person` on at most 5 notes,
  to say in a phrase what each one is. Don't quote private details.

2. Build a single-page dashboard as an artifact
- Clean, modern, readable on a phone. Use Chart.js from cdnjs. Light and dark mode.
- Header: the period (period.from to period.to) and 4 KPI cards: activity score with % change vs the
  previous period (totals.prev_score), events, active days, new notes (new_notes.count). Show changes as
  green ▲ or red ▼ badges.
- "Where my attention went": a stacked bar chart over `series`, one stack per {unit}, one color per topic
  in `series_topics`, plus grey for "other" and "untagged", using each bucket's `by_topic` values (they add
  up to that {unit}'s score). Mark the busiest and quietest {unit}s.
- "What occupied me": horizontal bar charts for top.topics and top.projects, each bar with its change_pct
  badge. Label items with prev_score 0 as "new".
- "People": the top people by interactions (count vs prev), plus top.people.
- "Notes": the most-touched notes (top.notes), and the newly created ones (new_notes.latest).
- "Went quiet": went_quiet items with their last_seen date. These were active before and silent now.
- "How I work": small doughnut charts for totals.by_action and totals.by_client.

3. Write the retrospective (above the charts)
- 3–5 sharp insights, each tied to a specific number.
- What's rising, what's fading, and one thing that may deserve more attention, such as a quiet project or
  a person not contacted recently.
- 3 reflection questions for me.
- Be direct and specific. No filler.

4. Offer to save it
After the report, ask whether to save a short text version (insights and top lists) to my vault as
`Reviews/<today> Retrospective ({period})`, using the `write` tool. Only save if I say yes.
"""
