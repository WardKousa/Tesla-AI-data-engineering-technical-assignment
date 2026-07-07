"""Part 2, Step 4: the agent harness -- routes natural-language questions
over the structured Megapack logs to the five tools in agent/tools.py.

Two interchangeable routing modes over the SAME tools:

  LLM mode (used when ANTHROPIC_API_KEY is set): plain tool-calling
      against the Anthropic API (claude-haiku-4-5). The model receives the
      question plus the tool schemas, decides which tools to call, gets
      their JSON results back, and writes the final answer. The system
      prompt forbids answering from anything but tool results; the tools'
      structured errors make that enforceable rather than hoped-for.

  Fallback mode (no API key needed): a deterministic keyword router that
      maps the intent to the same tools and fills a text template. It
      REFUSES anything it cannot map, listing what it can do -- it never
      guesses. This keeps the harness runnable (and gradeable) offline.

Usage:
    python -m agent.harness "Summarize the thermal anomalies today"
"""

from __future__ import annotations

import json
import os
import sys

from agent import tools

MODEL = "claude-haiku-4-5"  # routing + phrasing only; the hard analysis is in the tools
MAX_TURNS = 8
MAX_TOKENS = 8000

TOOL_SCHEMAS = [
    {
        "name": "query_events",
        "description": "Fetch individual log events, filtered by subsystem, severity, "
                       "metric and/or time range. Call this to inspect specific log "
                       "lines or find events matching a condition.",
        "input_schema": {
            "type": "object",
            "properties": {
                "subsystem": {"type": "string", "enum": sorted(tools.SUBSYSTEMS)},
                "severity": {"type": "string", "enum": sorted(tools.SEVERITIES)},
                "metric": {"type": "string", "enum": sorted(tools.METRICS)},
                "start": {"type": "string", "description": "e.g. 2026-06-18 13:00:00"},
                "end": {"type": "string"},
                "limit": {"type": "integer", "description": "max rows, default 20"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "event_stats",
        "description": "Count events grouped by columns (subsystem, severity, date, "
                       "hour, metric, message_template), with optional filters. When a "
                       "metric filter is given, also returns min/max/mean of its value. "
                       "Call this for 'how many' and comparison questions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "group_by": {"type": "array", "items": {"type": "string"}},
                "subsystem": {"type": "string"},
                "severity": {"type": "string"},
                "metric": {"type": "string"},
                "date": {"type": "string", "description": "e.g. 2026-06-18"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "plot_signals",
        "description": "Draw a time-series chart (PNG) of one or more metrics, with "
                       "WARNING/ERROR/CRITICAL readings highlighted. Set "
                       "around_incident=true to centre the window on the most severe "
                       "incident. Call this whenever the user asks for a plot or chart; "
                       "report the returned chart_path to the user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "metrics": {"type": "array", "items": {"type": "string",
                                                       "enum": sorted(tools.METRICS)}},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "highlight_alerts": {"type": "boolean"},
                "around_incident": {"type": "boolean"},
            },
            "required": ["metrics"],
            "additionalProperties": False,
        },
    },
    {
        "name": "summarize_incident",
        "description": "Structured JSON summary of the most severe alert episode: "
                       "timeline, root cause, first-warning/trip/recovery timings, and "
                       "a ranked list of all episodes. Optionally restrict to episodes "
                       "starting on a given date. Call this for incident, anomaly and "
                       "root-cause questions.",
        "input_schema": {
            "type": "object",
            "properties": {"date": {"type": "string", "description": "e.g. 2026-06-18"}},
            "additionalProperties": False,
        },
    },
    {
        "name": "draft_service_ticket",
        "description": "Draft a service ticket (JSON + rendered text) for the most "
                       "severe issue in the logs: root cause, affected subsystems, "
                       "evidence, recommended actions, and an attached evidence chart "
                       "(attachment_chart). Call this when the user asks for a ticket; "
                       "mention the attachment path in your answer.",
        "input_schema": {"type": "object", "properties": {},
                         "additionalProperties": False},
    },
]

TOOL_FUNCS = {
    "query_events": tools.query_events,
    "event_stats": tools.event_stats,
    "plot_signals": tools.plot_signals,
    "summarize_incident": tools.summarize_incident,
    "draft_service_ticket": tools.draft_service_ticket,
}


def _system_prompt() -> str:
    lo, hi = tools._bounds()
    today = str(hi.date())
    return (
        "You are a diagnostics assistant for one Tesla Megapack site. You answer "
        "questions about its operational logs using ONLY the results of your tools.\n"
        f"Data dictionary: the log covers {lo} to {hi}; 'today' means {today} (the "
        "most recent day). Subsystems: BMS, Battery, Inverter, Site, Thermal. "
        "Severities: INFO, WARNING, ERROR, CRITICAL. "
        f"Metrics: {', '.join(sorted(tools.METRICS))}.\n"
        "Rules:\n"
        "- Every number, timestamp and event in your answer must come from a tool "
        "result from this conversation. Never estimate or invent values.\n"
        "- If the tools cannot provide what is asked (dates outside the log, metrics "
        "that do not exist, or topics beyond this site's logs), say plainly that you "
        "cannot answer and why, instead of guessing.\n"
        "- When a tool returns a chart_path, include that path in your answer.\n"
        "- Be concise and engineer-friendly: lead with the finding, then the evidence."
    )


def _execute(name: str, tool_input: dict) -> dict:
    """Run one tool; errors become structured results, never crashes."""
    try:
        return TOOL_FUNCS[name](**tool_input)
    except Exception as exc:  # surface to the model/router, don't die
        return {"error": f"{type(exc).__name__}: {exc}"}


def run_llm(question: str) -> dict:
    """Anthropic tool-calling loop: ask -> execute tool calls -> repeat."""
    import anthropic

    client = anthropic.Anthropic()
    messages = [{"role": "user", "content": question}]
    calls: list[dict] = []

    for _ in range(MAX_TURNS):
        response = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, system=_system_prompt(),
            tools=TOOL_SCHEMAS, messages=messages)
        if response.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []  # all tool results go back in ONE user message
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = _execute(block.name, block.input)
            calls.append({"tool": block.name, "input": block.input, "result": result})
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": json.dumps(result, default=str),
                            "is_error": "error" in result})
        messages.append({"role": "user", "content": results})

    answer = "".join(b.text for b in response.content if b.type == "text")
    return {"mode": "llm", "model": MODEL, "question": question,
            "tool_calls": calls, "answer": answer}


# ---------------------------------------------------------------- fallback
KEYWORD_METRICS = [("coolant", "coolant_flow"), ("flow", "coolant_flow"),
                   ("temperature", "temperature"), ("temp", "temperature"),
                   ("fan", "fan_speed"), ("soc", "soc"), ("voltage", "voltage"),
                   ("frequency", "grid_frequency"), ("output", "ac_output")]

CAPABILITIES = ("summarize incidents/anomalies (optionally 'today'), plot metrics "
                "(e.g. 'plot coolant flow and temperature around the incident'), "
                "count events ('how many errors per subsystem'), and draft a service "
                "ticket for the most severe issue")


def run_fallback(question: str) -> dict:
    """Deterministic keyword router over the same tools; refuses the unknown."""
    q = question.lower()
    calls: list[dict] = []

    def call(name: str, **kwargs) -> dict:
        result = _execute(name, kwargs)
        calls.append({"tool": name, "input": kwargs, "result": result})
        return result

    today = str(tools._bounds()[1].date())
    if "ticket" in q:
        ticket = call("draft_service_ticket")
        answer = ticket.get("rendered_text", ticket.get("error", ""))
    elif any(w in q for w in ("plot", "chart", "graph")):
        metrics = list(dict.fromkeys(m for kw, m in KEYWORD_METRICS if kw in q)) \
            or ["coolant_flow", "temperature"]
        result = call("plot_signals", metrics=metrics, highlight_alerts=True,
                      around_incident="incident" in q)
        answer = (f"Chart saved to {result['chart_path']} covering "
                  f"{result['window'][0]} to {result['window'][1]}; "
                  "WARNING/ERROR/CRITICAL readings are highlighted."
                  if "chart_path" in result else f"Cannot plot: {result['error']}")
    elif any(w in q for w in ("summar", "anomal", "incident", "cause", "happened")):
        summary = call("summarize_incident", date=today if "today" in q else None)
        if "error" in summary:
            answer = f"Cannot summarize: {summary['error']}"
        else:
            ep, t = summary["episode"], summary["timings"]
            answer = (f"Most severe episode ({ep['start']} to {ep['end']}, "
                      f"{ep['classification']}): issue types "
                      f"{', '.join(ep['issue_types'])} across "
                      f"{', '.join(ep['subsystems'])}. Root cause: "
                      f"{summary['root_cause']} First warning {t['first_warning']}, "
                      f"trip {t['trip']} ({t['warning_to_trip_min']:.1f} min later), "
                      f"recovered {t['recovered']} "
                      f"(downtime {t['downtime_min']:.0f} min).")
    elif any(w in q for w in ("how many", "count", "per subsystem", "per severity")):
        stats = call("event_stats", group_by=["subsystem", "severity"])
        lines = [f"  {c['subsystem']:<9} {c['severity']:<9} {c['n_events']}"
                 for c in stats["counts"]]
        answer = f"Event counts ({stats['total']} total):\n" + "\n".join(lines)
    else:
        answer = ("I can't answer that with my available tools. I can: "
                  f"{CAPABILITIES}. (In LLM mode -- set ANTHROPIC_API_KEY -- freer "
                  "questions over the same data are supported.)")
    return {"mode": "fallback", "question": question,
            "tool_calls": calls, "answer": answer}


def answer(question: str) -> dict:
    """Route to LLM mode when a key is configured; degrade gracefully if not."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return run_llm(question)
        except Exception as exc:
            print(f"[harness] LLM mode failed ({type(exc).__name__}: {exc}); "
                  "falling back to the deterministic router.", file=sys.stderr)
    return run_fallback(question)


def truncate_for_display(value, max_items: int = 8):
    """Shorten long lists inside a JSON-able structure for readable output."""
    if isinstance(value, dict):
        return {k: truncate_for_display(v, max_items) for k, v in value.items()}
    if isinstance(value, list) and len(value) > max_items:
        return ([truncate_for_display(v, max_items) for v in value[:max_items]]
                + [f"... ({len(value) - max_items} more items truncated)"])
    if isinstance(value, list):
        return [truncate_for_display(v, max_items) for v in value]
    return value


def main() -> None:
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) < 2:
        print('usage: python -m agent.harness "your question"')
        raise SystemExit(1)
    result = answer(" ".join(sys.argv[1:]))
    print(f"[mode: {result['mode']}]")
    for call in result["tool_calls"]:
        print(f"  tool: {call['tool']}({json.dumps(call['input'], default=str)})")
    print()
    print(result["answer"])
    # The structured output the assignment asks for, alongside the text.
    print("\n--- JSON summary (tool results) " + "-" * 36)
    for call in result["tool_calls"]:
        print(f"\n[{call['tool']}]")
        print(json.dumps(truncate_for_display(call["result"]), indent=2,
                         default=str, ensure_ascii=False))


if __name__ == "__main__":
    main()
