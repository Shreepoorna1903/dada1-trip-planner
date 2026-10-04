"""Trip planner: the structured itinerary.

To be used to extract one typed itinerary from the conversation.

Please note `cost` is a STRING, not a number, so that
"unknown" is something the itinerary can actually say, and so a price can carry
where it came from. If it were a float the model would have to invent a figure
to fill it, and a made-up price looks exactly like a real one.
"""

from typing import Literal

from pydantic import BaseModel, Field

from datetime import datetime
from zoneinfo import ZoneInfo


class Activity(BaseModel):
    """One thing the traveller does."""
    time: str = Field(description="When it starts, e.g. '09:30' or 'morning'")
    name: str = Field(description="What it is, e.g. 'Museum of Fine Arts'")
    kind: Literal["sight", "food", "event", "travel", "other"] = Field(
        description="Which tool this came from: sight, food, event, travel, other")
    cost: str = Field(
        description="'free' only when something actually says it is free; a price "
                    "a tool gave you, marked with where it came from, like "
                    "'$45-120 (Ticketmaster)' or '$27 (blog, unconfirmed)'; or "
                    "'unknown'. Most things will be 'unknown' — that is the honest "
                    "answer, and an unknown price is never 0.")
    source: str = Field(description="The URL this came from, or 'none' if there isn't one")


# TODO: write DayPlan. One day of the trip: a date, what the weather said, and a
# list of Activity. An itinerary is a list of days, each holding a list of
# activities — one level more nesting than W03a.2.
class DayPlan(BaseModel):
    """One day of the trip."""
    date: str = Field(description="The date as YYYY-MM-DD, or 'unspecified' if the "
                                  "conversation never fixed one")
    weather: str = Field(description="What get_weather reported for this day, e.g. "
                                     "'light rain, high 14°C'; or 'no forecast available' "
                                     "if the tool said the date was out of range; or "
                                     "'not checked'. Never describe typical seasonal weather.")
    activities: list[Activity] = Field(description="The activities for this day, in order")


# TODO: write Itinerary. The city, a list of DayPlan, and an `unknowns` list of
# everything the tools could not confirm. Give every field a Field(description=...):
# those descriptions are what the model reads when it fills the model in.
class Itinerary(BaseModel):
    """A complete plan for a trip."""
    city: str = Field(description="The city, as resolved by the tools, e.g. "
                                  "'Boston, Massachusetts, United States'")
    days: list[DayPlan] = Field(description="One entry per day of the trip, in date order")
    unknowns: list[str] = Field(description="Everything the tools could not confirm: "
                                            "missing prices, opening hours, journey times, "
                                            "forecasts out of range, unverified 'free' claims. "
                                            "One short sentence each.")



ITINERARIES = []


def build_itinerary(model, result) -> Itinerary:
    """Extract a structured itinerary from a finished planning conversation.

    This is `file_ticket` from W03b.1 pointed at a trip. Build the transcript from
    result["messages"], call model.with_structured_output(Itinerary) on it, append
    to ITINERARIES, and return it.
    """
    # TODO: implement

    roles = {"human": "User", "ai": "Agent", "tool": "Tool"}
    lines = []
    for m in result["messages"]:
        role = roles.get(m.type)
        text = m.text
        if not role or not text:
            continue
        if m.type == "tool":
            role = f"Tool ({m.name})"
        lines.append(f"{role}: {text}")
    transcript = "\n\n".join(lines)
    today = datetime.now(ZoneInfo("America/New_York")).strftime("%A, %Y-%m-%d")
    prompt = (
        f"Today is {today}. Resolve any relative dates ('tomorrow') against it.\n"
        "Extract the final trip itinerary from this conversation. If the user asked "
        "for changes, use the latest agreed version of the plan, not an earlier one.\n"
        "Make one activity per place. If the plan offers alternatives ('X or Y'), list "
        "each as its own activity with the same time. Give each the source URL it was "
        "cited with, and use the plan's time of day (morning, lunch, afternoon, dinner).\n"
        "Use only what the transcript states. Do not invent times, prices, or URLs. "
        "A cost is 'free' only if a tool result says so, a price only if a tool "
        "result states one (with its source), and otherwise 'unknown'; a missing "
        "price or a price of 0 is never 'free'. If a tool result states a price for "
        "something at a place (e.g. a $9 dish), put it in cost with its source, like "
        "'$9 noodle bowl (The Infatuation)'. Put everything the tools could not "
        "confirm in unknowns.\n\n"
        f"Conversation:\n{transcript}"
    )

    itinerary = model.with_structured_output(Itinerary).invoke(prompt)
    ITINERARIES.append(itinerary)
    return itinerary
