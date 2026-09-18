"""The fixed action set the model may call during discovery.

Targets are named the way the artifact will name them (frame + role + accessible
name, or frame + role + anchor text), so what the model used is exactly what
replay resolves. There is no free-form "run javascript" or "click x,y" tool.
"""
from __future__ import annotations

from .llm import ToolSpec

_TARGET_PROPS = {
    "frame": {"type": "string", "description": "Frame path exactly as shown in the observation, e.g. 'main' or 'nav'. Use 'top' for the top document."},
    "role": {"type": "string", "description": "Control role from the observation: textbox, button, link, combobox, checkbox, radio, cell."},
    "name": {"type": ["string", "null"], "description": "Accessible name from the observation (name=\"...\"). Omit or null when the control has no name."},
    "anchor": {"type": ["string", "null"], "description": "For unnamed controls: the anchor text from the observation (anchor=\"...\"), i.e. the label text before it."},
    "reason": {"type": "string", "description": "One sentence: why this action advances the goal."},
}


def _target(extra: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": {**_TARGET_PROPS, **extra}, "required": ["frame", "role", "reason", *required]}


TOOLS: list[ToolSpec] = [
    ToolSpec("navigate", "Open a URL in the top document. Only allowlisted origins/routes are permitted.",
             {"type": "object", "properties": {"url": {"type": "string"}, "reason": _TARGET_PROPS["reason"]}, "required": ["url", "reason"]}),
    ToolSpec("click", "Click a button or link.", _target({}, [])),
    ToolSpec("type_text", "Clear a text control and type into it. For credentials type the placeholder given in the instructions, e.g. {{secret:NAME}}, verbatim.",
             _target({"text": {"type": "string"},
                      "param_name": {"type": ["string", "null"], "description": "If this value comes from the goal and would differ per invocation (an id, an amount), a snake_case parameter name for it, e.g. member_id. Omit or null for constants and secrets."}},
                     ["text"])),
    ToolSpec("select_option", "Choose an option in a combobox/select by its visible label.",
             _target({"option": {"type": "string"}, "param_name": {"type": ["string", "null"], "description": "As for type_text."}}, ["option"])),
    ToolSpec("press_key", "Press a key (e.g. Enter) while a control is focused.", _target({"key": {"type": "string"}}, ["key"])),
    ToolSpec("extract", "Read a value from the page into a named output. Use table_cell for a value in a table (row text + column header) or label_value for 'Label | value' rows.",
             {"type": "object", "properties": {
                 "frame": _TARGET_PROPS["frame"],
                 "output_name": {"type": "string", "description": "snake_case output name, e.g. savings_balance"},
                 "strategy": {"type": "string", "enum": ["table_cell", "label_value"]},
                 "row_anchor": {"type": ["string", "null"], "description": "table_cell: text that identifies the row, e.g. 'Savings'"},
                 "column_header": {"type": ["string", "null"], "description": "table_cell: header text of the column, e.g. 'Balance'"},
                 "label": {"type": ["string", "null"], "description": "label_value: the label cell text, e.g. 'Status'"},
                 "value_type": {"type": "string", "enum": ["string", "money", "integer", "number"]},
                 "reason": _TARGET_PROPS["reason"]},
              "required": ["frame", "output_name", "strategy", "value_type", "reason"]}),
    ToolSpec("handle_dialog", "Declare how a JavaScript dialog whose message matches a pattern must be handled from now on. Unknown dialogs are always dismissed.",
             {"type": "object", "properties": {"pattern": {"type": "string", "description": "Regex matched against the dialog message."},
                                               "action": {"type": "string", "enum": ["accept", "dismiss"]},
                                               "reason": _TARGET_PROPS["reason"]},
              "required": ["pattern", "action", "reason"]}),
    ToolSpec("done", "Declare the goal met. Only after the required state is visible and all outputs were extracted.",
             {"type": "object", "properties": {
                 "capability_id": {"type": "string", "description": "snake_case name for this reusable capability, e.g. member_savings_balance_lookup"},
                 "description": {"type": "string", "description": "One sentence describing what the capability does, its inputs and outputs."},
                 "summary": {"type": "string", "description": "What was achieved and what the outputs are."}},
              "required": ["capability_id", "description", "summary"]}),
    ToolSpec("escalate", "Stop and hand the live session to a human operator because you cannot safely proceed.",
             {"type": "object", "properties": {"reason": {"type": "string"}}, "required": ["reason"]}),
]

TOOL_NAMES = {t.name for t in TOOLS}
