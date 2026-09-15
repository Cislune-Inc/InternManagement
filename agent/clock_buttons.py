"""Small Slack controls for deterministic clock replies; text remains a fallback."""
import re


def blocks(message):
    controls = []
    if message.startswith(("Plan lunch within", "Lunch is due now.")):
        controls = [("Start lunch", "dp_clock_lunch", None),
                    ("Already took lunch", "dp_clock_fix_lunch", None),
                    ("Need help", "dp_clock_report", None)]
    elif message.startswith(("Unpaid lunch recorded", "Your 30-minute lunch minimum is complete")):
        controls = [("Back to work", "dp_clock_back", None), ("Correct lunch", "dp_clock_fix_lunch", None)]
    elif message.startswith("*Lunch correction preview"):
        match = re.search(r"confirm lunch ([0-9a-f]{8})", message)
        if match:
            controls = [("Confirm lunch", "dp_clock_confirm_lunch", match[1]),
                        ("Cancel", "dp_clock_cancel_lunch", None)]
    elif message.startswith(("Did work finish at", "Stop work now:", "Your clock stopped after")):
        match = re.search(r"confirm stop (\d{4}-\d{2}-\d{2} [0-9a-f]{8})", message)
        if match:
            controls = [("Yes, finished then", "dp_clock_confirm_stop", match[1]), ("Correct finish / missing work", "dp_clock_report", None)]
    elif message.startswith(("Clocked out at", "Already clocked out.", "Lunch end recorded.", "Your finish time is confirmed")):
        controls = [("Correct lunch", "dp_clock_fix_lunch", None), ("Report an issue", "dp_clock_report", None)]
        match = re.search(r"confirm day (\d{4}-\d{2}-\d{2} [0-9a-f]{8})", message)
        if match:
            controls = [("Hours and lunch look right", "dp_clock_confirm_day", match[1]),
                        ("Correct lunch", "dp_clock_fix_lunch", None),
                        ("Report an issue", "dp_clock_report", None)]
    if not controls:
        return None
    elements = []
    for label, action, value in controls:
        element = {"type": "button", "action_id": action, "text": {"type": "plain_text", "text": label}}
        if value:
            element["value"] = value
        elements.append(element)
    return [{"type": "section", "text": {"type": "mrkdwn", "text": message[:3000]}},
            {"type": "actions", "elements": elements}]
