"""Trip planner: Tools

Two rules, the same ones from the Husky Tech agent:
  1. Return a readable string. Never raise, never return a bare dict.
  2. When you have nothing useful, SAY SO in the return value. "No results —
     try a nearby larger city" is a sentence the model can act on.
"""

import os
from datetime import datetime, timedelta

import requests
from langchain.tools import tool
from langchain_tavily import TavilySearch
import time
from zoneinfo import ZoneInfo

# Use these URLs for their corresponding tools
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
EVENTS_URL = "https://app.ticketmaster.com/discovery/v2/events.json"

FORECAST_DAYS = 16          # all Open-Meteo will forecast: today + 15
MAX_RESULTS = 3
MAX_EVENTS = 10
LOCAL_TZ = ZoneInfo("America/New_York")   

# Open-Meteo returns a WMO code, not words. Only the common ones are listed;
# anything else falls through to "code N", which will keep the agent honest.
WEATHER_CODES = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow",
    75: "heavy snow", 77: "snow grains", 80: "rain showers", 81: "rain showers",
    82: "violent rain showers", 85: "snow showers", 86: "snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with hail",
}


def _search(query: str) -> str:
    """Run one web search and format the hits. Shared by the four find_* tools."""
    try:
        results = TavilySearch(max_results=MAX_RESULTS).invoke({"query": query})
    except Exception as e:
        return f"The search failed ({e}). Tell the user you could not look this up."

    hits = results.get("results", []) if isinstance(results, dict) else []
    if not hits:
        return (f"No results for '{query}'. Try a nearby larger city, or tell the "
                f"user you could not find anything — do not fill the gap from memory.")

    lines = []
    for i, h in enumerate(hits, 1):
        snippet = " ".join((h.get("content") or "").split())[:300]
        lines.append(f"{i}. {h.get('title', 'untitled')}\n   {h.get('url', '')}\n   {snippet}")
    return "\n".join(lines) + (
        "\nThese snippets are the only evidence you have. They do not include "
        "opening hours, and a price counts only if it is written above.")


def _geocode(city: str) -> tuple[str, float, float, str]:
    """City name -> (label, latitude, longitude, ambiguity_note). Written for you.

    Raises ValueError if nothing matches. The ambiguity note is the interesting
    part: this API ranks by population, so "Cambridge" puts England ahead of
    Massachusetts and "Portland" puts Oregon ahead of Maine.
    """
    r = requests.get(GEOCODE_URL, params={"name": city.strip(), "count": 5,
                                          "language": "en", "format": "json"},
                     timeout=15)
    r.raise_for_status()
    results = (r.json() or {}).get("results") or []
    if not results:
        raise ValueError(f"No place matches '{city}'.")

    def describe(r):
        return ", ".join(x for x in (r["name"], r.get("admin1"), r.get("country")) if x)

    top = results[0]
    label = describe(top)

    # Every US city shares its name with a hamlet somewhere; "Boston, Georgia (pop. 1,400)"
    # is noise, and an agent that hedges on every city stops being useful.
    top_pop = top.get("population") or 0
    rivals = [describe(o) for o in results[1:4]
              if o["name"].lower() == top["name"].lower()
              and (o.get("population") or 0) >= 0.10 * top_pop]
    note = (f" NOTE: '{city}' is ambiguous — could also be {'; '.join(rivals)}. "
            f"Confirm which one before planning." if rivals else "")
    return label, top["latitude"], top["longitude"], note


# NOTE: The tools already have some docstring descriptions
# but you can modify them if you find the descriptions insufficient or inaccurate.
@tool
def get_weather(city: str, date: str = "") -> str:
    """Get the weather forecast for a city on a date (YYYY-MM-DD).

    Only the next 16 days can be forecast. For any date beyond that the forecast
    does not exist, and this tool will say so. Say that plainly instead of
    describing what the season is usually like. Leave date empty for today.

    The city name is resolved to a real place, and this tool tells you WHICH
    place it picked. If other places share the name, it says so: check it matches
    what the user meant before you plan anything.
    """
    if date:
        try:
            datetime.strptime(date.strip(), "%Y-%m-%d")
            date = date.strip()
        except ValueError:
            return f"'{date}' is not a valid date. Use YYYY-MM-DD, or leave it empty for today."

    try:
        label, lat, lon, note = _geocode(city)
    except ValueError as e:
        return f"{e} Check the spelling, or try a nearby larger city."
    except requests.RequestException as e:
        return (f"Could not look up '{city}' (geocoding error: {e}). "
                f"Tell the user the weather is unavailable right now; do not guess it.")

    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": ("weather_code,temperature_2m_max,temperature_2m_min,"
                  "precipitation_probability_max,precipitation_sum"),
        "timezone": "auto",
        "forecast_days": FORECAST_DAYS,
    }
    for attempt in range(3):
        try:
            response = requests.get(FORECAST_URL, params=params, timeout=15)
            if response.status_code == 429 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            response.raise_for_status()
            data = response.json() or {}
            break
        except (requests.RequestException, ValueError) as e:
            return (f"The forecast service failed for {label} ({e}). "
                    f"Tell the user you could not get the weather; do not guess it.{note}")

    daily = data.get("daily") or {}
    days = daily.get("time") or []

    if not days:
        return f"The forecast service returned no dates for {label}. Do not guess the weather.{note}"

    target = date or days[0]

    if target not in days:
        when = "in the past" if target < days[0] else "too far ahead"
        return (f"No forecast exists for {target} ({when}). Open-Meteo only covers "
                f"{days[0]} to {days[-1]} for {label}. Tell the user plainly; "
                f"do not describe typical seasonal weather instead.{note}")

    i = days.index(target)

    def val(key):
        vals = daily.get(key) or []
        return vals[i] if i < len(vals) else None

    def temp(c):
        return "not available" if c is None else f"{c:.0f}°C ({c * 9 / 5 + 32:.0f}°F)"

    code = val("weather_code")
    sky = ("not available" if code is None
           else WEATHER_CODES.get(code, f"code {code}"))
    prob = val("precipitation_probability_max")
    rain = val("precipitation_sum")

    lines = [f"Forecast for {label} on {target}"
             f"{' (today)' if target == days[0] else ''}:"]
    if note:
        lines.append(note.strip())

    lines += [
        f"- Conditions: {sky}",
        f"- High: {temp(val('temperature_2m_max'))}",
        f"- Low: {temp(val('temperature_2m_min'))}",
        f"- Chance of precipitation: {'not available' if prob is None else f'{prob}%'}",
        f"- Expected precipitation: {'not available' if rain is None else f'{rain} mm'}",
        f"Forecast window available: {days[0]} to {days[-1]}.",
        "Source: Open-Meteo (https://open-meteo.com)",
    ]
    return "\n".join(lines)

@tool
def find_attractions(city: str, interest: str = "") -> str:
    """Find sights, museums, parks and landmarks in a city.

    interest narrows it, e.g. "art", "history", "outdoors", "free".
    Use this for things to SEE. For food use find_restaurants; for things
    happening on a particular date use find_events.
    """
    # TODO: implement. Return a readable string

    interest = (interest or "").strip()

    if interest:
        query = f"best {interest} attractions and sights to visit in {city.strip()}"
    else:
        query = f"best attractions, museums, parks and landmarks to visit in {city.strip()}"

    return _search(query)


@tool
def find_restaurants(city: str, budget: str = "") -> str:
    """Find places to eat in a city.

    budget narrows it, e.g. "cheap", "mid-range", "fine dining".
    Use this for food and drink only. It does not return opening hours or
    current prices, so do not promise either.

    Returns web search snippets with source URLs. They contain no opening hours
    and no reliable prices. "Open now"/"Closed now" is a stale snapshot from when
    the page was crawled, and $-$$$$ symbols are price bands, not prices.
    """
    # TODO: implement. Return a readable string; never raise.

    budget = (budget or "").strip()
    city = city.strip()
    if budget:
        query = f"best {budget} restaurants in {city}"
    else:
        query = f"best restaurants to eat in {city}"
    return _search(query)+ ("\nIgnore any 'Open now' or 'Closed now' text: it is a crawl-time snapshot, "
        "not opening hours. Treat $ symbols as a rough price band only; never turn "
        "them into a dollar figure. Report cost as unknown unless a snippet states "
        "an actual price.")

@tool
def find_events(city: str, date: str = "", category: str = "") -> str:
    """Find real ticketed events in a city: concerts, sport, theatre, festivals.

    date is YYYY-MM-DD and means that day in the venue's own local time; leave
    it empty for the week ahead. Unlike the search tools this returns structured
    records: real dates, real venues, real links.

    For a range such as a weekend, call once per date.

    category narrows by type. The five broad ones always work: "music", "sports",
    "arts & theatre", "film", "miscellaneous". Many narrower ones work too —
    "comedy", "rock", "jazz", "classical", "hip-hop", "country", "pop",
    "basketball", "hockey", "theatre", "musical", "dance", "family". Leave it
    empty for everything.

    But some plausible-sounding words match nothing at all ("opera", "magic"),
    and a category the API does not recognise returns ZERO EVENTS rather than an
    error — identical to a genuinely quiet weekend. Prefer a word from the list
    above, and never report "there is nothing on" from an empty filtered result
    without checking again without the filter.

    MOST EVENTS COME BACK WITH NO PRICE. The API documents a price field and
    fills it in for only a small fraction of events, under 10% in every city
    tested. "price not published" means exactly that. Do not substitute a guess,
    and do not read a missing price as free admission. A price published as 0 is
    a placeholder, not a free ticket.

    This only covers events sold through Ticketmaster. A quiet result does not
    mean nothing is happening. Say that rather than implying the city is empty.
    """
    api_key = os.getenv("TICKETMASTER_API_KEY")
    if not api_key:
        return ("Event search is unavailable (no Ticketmaster API key configured). "
                "Tell the user you cannot look up events right now.")

    city = city.strip()
    date = (date or "").strip()
    category = (category or "").strip()
    today = datetime.now(LOCAL_TZ).date()

    if date:
        try:
            start = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            return f"'{date}' is not a valid date. Use YYYY-MM-DD, or leave it empty for the week ahead."
        end = start
    else:
        start = datetime.now(LOCAL_TZ).replace(tzinfo=None)
        end = start + timedelta(days=7)

    params = {
        "apikey": api_key,
        "city": city,
        "localStartEndDateTime": f"{start:%Y-%m-%dT00:00:00},{end:%Y-%m-%dT23:59:59}",
        "sort": "date,asc",
        "size": MAX_EVENTS,
    }

    if category:
        params["classificationName"] = category

    try:
        r = requests.get(EVENTS_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json() or {}
        events = (data.get("_embedded") or {}).get("events") or []
        total = (data.get("page") or {}).get("totalElements", len(events))
    except (requests.RequestException, ValueError) as e:
        return f"The event search failed ({e}). Tell the user you could not look up events."

    window = date or f"{start:%Y-%m-%d} to {end:%Y-%m-%d}"

    if not events:
        past = ""
        if date and start.date() < today:
            past = (f" Note: {date} has already passed. Ticketmaster only lists "
                    f"upcoming events, so past dates return nothing.")
        if category:
            return (f"No Ticketmaster events found in {city} for {window} with category "
                    f"'{category}'. Either nothing in that category is on, or the category "
                    f"was not recognised (unknown categories also return zero results). "
                    f"Search again without the category before telling the user nothing is on.{past}")
        return (f"No Ticketmaster events found in {city} for {window}. This only covers "
                f"Ticketmaster; do not tell the user the city has nothing happening.{past}")

    def price(ev):
        ranges = ev.get("priceRanges") or []
        if not ranges:
            return "price not published"
        lo, hi = ranges[0].get("min"), ranges[0].get("max")
        if not lo and not hi:
            return "price not published (listed as 0, a placeholder, NOT free)"
        cur = ranges[0].get("currency", "")
        return f"{lo}-{hi} {cur}" if lo != hi else f"{lo} {cur}"

    lines = [f"Ticketmaster events in {city} for {window} "
             f"(showing {len(events)} of {total}, earliest first):"]
    if total > len(events):
        if date:
            hint = "later times that day may be missing."
            if not category:
                hint += " Add a category to narrow the results."
            lines.append(f"More events exist on {date} than shown; {hint}")
        else:
            lines.append("More events exist than shown, so later days in this window may be "
                         "missing. Search a specific date to see them.")
    for i, ev in enumerate(events, 1):
        s = (ev.get("dates") or {}).get("start") or {}
        when = s.get("localDate", "date unknown")
        if s.get("localTime"):
            when += f" {s['localTime'][:5]}"
        venues = (ev.get("_embedded") or {}).get("venues") or [{}]
        venue = venues[0].get("name", "venue not listed")
        lines.append(f"{i}. {ev.get('name', 'untitled')}\n"
                     f"   {when} at {venue}\n"
                     f"   Price: {price(ev)}\n"
                     f"   {ev.get('url', '')}")
    lines.append("'price not published' means unknown, not free. Do not guess a price.")
    return "\n".join(lines)

@tool
def get_transit_info(city: str) -> str:
    """Get general guidance on getting around a city: subway/metro, buses,
    commuter rail, passes and fare types.

    Returns web search snippets with source URLs: written guidance only. NOT
    timetables, NOT live service status, NOT journey times. If the user asks how
    long a specific trip takes or when the next train leaves, say you cannot look
    that up rather than estimating it. Fares in snippets may be out of date;
    quote them only as written, with the source.
    """
    # TODO: implement. Return a readable string
    query = f"{city.strip()} public transportation guide: subway, bus, passes and fares"
    return _search(query) + (
        "\nThis is general guidance only. It contains no timetables and no journey "
        "times. If asked how long a trip takes, say you cannot look that up; do not "
        "estimate from distance, number of stops, or general knowledge.")
    


