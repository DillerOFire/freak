"""Pure HTML rendering for the telemetry dashboard. No SQLite queries."""

import html
import json
from typing import Any

from bot.telemetry.analysis import build_context_engineering_suggestions


_STATUS_LABELS = {
    "success": "Success",
    "no_reply": "No reply",
    "invalid_json": "Invalid JSON",
    "validation_error": "Validation error",
    "empty_content": "Empty content",
    "exception": "Exception",
}

_STATUS_COLORS = {
    "success": "#2e7d32",
    "no_reply": "#1565c0",
    "invalid_json": "#c62828",
    "validation_error": "#c62828",
    "empty_content": "#c62828",
    "exception": "#c62828",
}


def _esc(value: Any) -> str:
    return html.escape(str(value)) if value is not None else ""


def _pre_json(value: Any) -> str:
    return _esc(json.dumps(value, ensure_ascii=False, indent=2))


def _rate(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.1f}%"


def _num(value: float | int | None) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)


def _status_badge(status: str) -> str:
    label = _STATUS_LABELS.get(status, _esc(status))
    color = _STATUS_COLORS.get(status, "#616161")
    return f'<span class="badge" style="background:{color}">{label}</span>'


def _filters_html(chats: list[int], filters: dict) -> str:
    chat_id = filters.get("chat_id")
    status = filters.get("status") or "all"
    source = filters.get("source") or "all"
    limit = filters.get("limit", 100)

    chat_options = ['<option value="all">All chats</option>']
    for cid in chats:
        selected = " selected" if str(chat_id) == str(cid) else ""
        chat_options.append(
            f'<option value="{_esc(cid)}"{selected}>{_esc(cid)}</option>'
        )

    status_options = []
    for value in ["all", "success", "no_reply", "invalid_json", "validation_error", "empty_content", "exception"]:
        selected = " selected" if status == value else ""
        label = _STATUS_LABELS.get(value, "All statuses" if value == "all" else value)
        status_options.append(f'<option value="{value}"{selected}>{label}</option>')

    source_options = []
    for value in ["all", "message", "daily_task"]:
        selected = " selected" if source == value else ""
        label = "All sources" if value == "all" else value
        source_options.append(f'<option value="{value}"{selected}>{label}</option>')

    return f"""
    <form class="filters" method="get" action="/telemetry">
        <label>Chat
            <select name="chat_id">{"".join(chat_options)}</select>
        </label>
        <label>Status
            <select name="status">{"".join(status_options)}</select>
        </label>
        <label>Source
            <select name="source">{"".join(source_options)}</select>
        </label>
        <label>Limit
            <input type="number" name="limit" value="{_esc(limit)}" min="1" max="500">
        </label>
        <button type="submit">Apply</button>
    </form>
    """


def _summary_cards(summary: dict) -> str:
    cards = [
        ("Total events", _num(summary.get("total_events"))),
        ("Success rate", _rate(summary.get("success_rate"))),
        ("No-reply rate", _rate(summary.get("no_reply_rate"))),
        ("Failure rate", _rate(summary.get("failure_rate"))),
        ("Avg context chars", _num(summary.get("avg_context_chars"))),
        ("Avg retrieved memories", _num(summary.get("avg_retrieved_memory_count"))),
        ("Avg memory writes", _num(summary.get("avg_memory_write_count"))),
        ("Memory write success", _rate(summary.get("memory_write_success_rate"))),
    ]
    return "".join(
        f'<div class="card"><div class="card-title">{_esc(title)}</div>'
        f'<div class="card-value">{value}</div></div>'
        for title, value in cards
    )


def _suggestions_panel(events: list[dict]) -> str:
    suggestions = build_context_engineering_suggestions(events)
    items = "".join(f"<li>{_esc(s)}</li>" for s in suggestions)
    return f'<section class="panel"><h2>Suggestions</h2><ul class="suggestions">{items}</ul></section>'


def _memory_behavior_panel(summary: dict) -> str:
    topics = summary.get("top_memory_write_topics") or []
    if topics:
        topic_items = "".join(
            f"<li>{_esc(t.get('topic'))} ({_num(t.get('count'))})</li>"
            for t in topics
        )
    else:
        topic_items = "<li class=\"muted\">No memory writes recorded.</li>"

    examples = summary.get("recent_no_memory_examples") or []
    if examples:
        example_rows = "".join(
            f'<tr><td><a href="#event-{_esc(ex.get("id"))}">#{_esc(ex.get("id"))}</a></td>'
            f"<td>{_esc(ex.get('timestamp'))}</td>"
            f"<td>{_esc(ex.get('status'))}</td>"
            f"<td>{_num(ex.get('context_message_count'))}</td>"
            f"<td>{_num(ex.get('retrieved_memory_count'))}</td>"
            f"<td>{_esc(ex.get('last_trigger_text'))}</td></tr>"
            for ex in examples
        )
    else:
        example_rows = '<tr><td colspan="6" class="muted">No examples.</td></tr>'

    return f"""
    <section class="panel">
        <h2>Memory Behavior</h2>
        <h3>Top memory write topics</h3>
        <ul class="topics">{topic_items}</ul>
        <h3>Recent replies with no memorization</h3>
        <table class="thin">
            <thead><tr><th>ID</th><th>Timestamp</th><th>Status</th><th>Ctx msgs</th><th>Retrieved</th><th>Last trigger</th></tr></thead>
            <tbody>{example_rows}</tbody>
        </table>
    </section>
    """


def _event_details(event: dict) -> str:
    triggers = event.get("trigger_messages") or []
    last_triggers = triggers[-5:]
    trigger_rows = "".join(
        f'<tr><td>{_esc(m.get("sender"))} / {_esc(m.get("user_id"))}</td>'
        f"<td>{_esc(m.get('text'))}</td></tr>"
        for m in last_triggers
    ) or '<tr><td colspan="2" class="muted">None.</td></tr>'

    user_thoughts = event.get("used_user_thoughts") or {}
    general_memories = event.get("used_general_memories") or []

    thoughts_html = _pre_json(user_thoughts)
    memories_html = _pre_json(general_memories)

    response_messages = event.get("response_messages") or []
    reply_to = event.get("reply_to_message_id")
    response_html = _pre_json(response_messages)

    memory_writes = event.get("memory_writes") or []
    write_rows = "".join(
        f'<tr><td>{_esc(w.get("type"))}</td>'
        f'<td>{_status_badge(w.get("status", "pending"))}</td>'
        f"<td><pre>{_pre_json(w.get('arguments'))}</pre></td></tr>"
        for w in memory_writes
        if isinstance(w, dict)
    ) or '<tr><td colspan="3" class="muted">No memory writes.</td></tr>'

    return f"""
    <details>
        <summary>Inspect context, memories, response, and memorization</summary>
        <div class="detail-grid">
            <div class="detail-section">
                <h4>Trigger messages used</h4>
                <table class="thin">
                    <thead><tr><th>Sender / ID</th><th>Text</th></tr></thead>
                    <tbody>{trigger_rows}</tbody>
                </table>
            </div>
            <div class="detail-section">
                <h4>Memories used</h4>
                <h5>User thoughts</h5>
                <pre>{thoughts_html}</pre>
                <h5>General memories</h5>
                <pre>{memories_html}</pre>
            </div>
            <div class="detail-section">
                <h4>Response</h4>
                <p>Reply to: {_esc(reply_to)}</p>
                <pre>{response_html}</pre>
                <h5>Media</h5>
                <pre>{_pre_json(event.get("response_media") or {})}</pre>
            </div>
            <div class="detail-section">
                <h4>Memorized</h4>
                <table class="thin">
                    <thead><tr><th>Type</th><th>Status</th><th>Arguments</th></tr></thead>
                    <tbody>{write_rows}</tbody>
                </table>
            </div>
        </div>
    </details>
    """


def _events_table(events: list[dict]) -> str:
    rows = ""
    for event in events:
        rows += (
            f'<tr id="event-{_esc(event.get("id"))}">'
            f"<td>{_esc(event.get('timestamp'))}</td>"
            f"<td>{_esc(event.get('chat_id'))}</td>"
            f"<td>{_esc(event.get('source'))}</td>"
            f"<td>{_status_badge(event.get('status', 'unknown'))}</td>"
            f"<td>{_num(event.get('context_message_count'))}</td>"
            f"<td>{_num(event.get('retrieved_memory_count'))}</td>"
            f"<td>{_num(event.get('memory_write_count'))}/{_num(event.get('failed_memory_write_count'))}</td>"
            f"<td>{_num(event.get('response_message_count'))}</td>"
            f"<td>{_num(event.get('latency_ms'))}</td>"
            f"</tr>"
            f'<tr class="detail-row"><td colspan="9">{_event_details(event)}</td></tr>'
        )
    return f"""
    <section class="panel">
        <h2>Events</h2>
        <table class="events">
            <thead><tr>
                <th>Timestamp</th><th>Chat</th><th>Source</th><th>Status</th>
                <th>Ctx msgs</th><th>Retrieved</th><th>Writes ok/failed</th>
                <th>Resp msgs</th><th>Latency (ms)</th>
            </tr></thead>
            <tbody>{rows}</tbody>
        </table>
    </section>
    """


_CSS = """
* { box-sizing: border-box; }
body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #f5f5f7; color: #1d1d1f; }
.container { max-width: 1200px; margin: 0 auto; padding: 24px; }
h1 { margin: 0 0 4px; }
.subtitle { color: #555; margin: 0 0 16px; }
.links { margin: 12px 0; }
.links a { margin-right: 16px; }
.filters { display: flex; flex-wrap: wrap; gap: 12px; align-items: end; background: #fff; padding: 16px; border-radius: 8px; margin-bottom: 16px; }
.filters label { display: flex; flex-direction: column; font-size: 13px; color: #555; }
.filters select, .filters input { margin-top: 4px; padding: 6px; border: 1px solid #ccc; border-radius: 4px; }
.filters button { padding: 8px 16px; border: none; background: #0071e3; color: #fff; border-radius: 4px; cursor: pointer; }
.cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 12px; margin-bottom: 16px; }
.card { background: #fff; padding: 16px; border-radius: 8px; }
.card-title { font-size: 12px; color: #666; text-transform: uppercase; letter-spacing: .5px; }
.card-value { font-size: 24px; font-weight: 600; margin-top: 4px; }
.panel { background: #fff; padding: 16px; border-radius: 8px; margin-bottom: 16px; }
.panel h2 { margin-top: 0; }
.panel h3 { margin-bottom: 4px; }
.suggestions { padding-left: 20px; }
.suggestions li { margin: 4px 0; }
.topics { padding-left: 20px; }
.muted { color: #999; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #eee; }
th { background: #fafafa; }
.badge { color: #fff; padding: 2px 8px; border-radius: 10px; font-size: 11px; }
.events tbody tr:hover { background: #fafafa; }
.detail-row td { padding: 0; }
.detail-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 16px; padding: 16px; background: #fafafa; }
.detail-section h4, .detail-section h5 { margin: 8px 0 4px; }
pre { background: #fff; padding: 8px; border-radius: 4px; overflow-x: auto; font-size: 12px; border: 1px solid #eee; max-height: 240px; }
details summary { cursor: pointer; padding: 8px 16px; font-weight: 500; }
"""


def render_dashboard_html(events: list[dict], chats: list[int], filters: dict) -> str:
    """Render the complete telemetry dashboard HTML document."""
    from bot.telemetry.analysis import summarize_telemetry

    summary = summarize_telemetry(events)

    links_parts = [
        '<a href="/telemetry/export.json">Export current view as JSON</a>'
    ]
    if events:
        links_parts.append(
            f'<a href="/telemetry/event/{_esc(events[0].get("id"))}.json">Raw latest event JSON</a>'
        )
    links = '<div class="links">' + "".join(links_parts) + "</div>"

    if not events:
        body_events = (
            '<section class="panel"><p>No telemetry recorded for these filters yet.</p></section>'
        )
    else:
        body_events = _events_table(events)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Bot Telemetry Dashboard</title>
<style>{_CSS}</style>
</head>
<body>
<div class="container">
    <h1>Bot Telemetry Dashboard</h1>
    <p class="subtitle">Inspect what context and memories the bot used, what it memorized, and what to improve.</p>
    {links}
    {_filters_html(chats, filters)}
    <div class="cards">{_summary_cards(summary)}</div>
    {_suggestions_panel(events)}
    {_memory_behavior_panel(summary)}
    {body_events}
</div>
</body>
</html>
"""
