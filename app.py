#!/usr/bin/python3
from flask import Flask, render_template, make_response, request, jsonify, \
    redirect, send_from_directory
from flask_caching import Cache
import json
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
import requests
import icalendar
import datetime
from dateutil.rrule import rrulestr
from pprint import pprint

# configuration
DEBUG = os.environ.get("APP_DEBUG", "true").lower() == "true"
PORT = int(os.environ.get("PORT", "5000"))

MAXIMUM_THREADS = 100

# constants
HERE = os.path.dirname(__name__) or "."
DEFAULT_SPECIFICATION_PATH = os.path.join(HERE, "default_specification.json")
TEMPLATE_FOLDER_NAME = "templates"
TEMPLATE_FOLDER = os.path.join(HERE, TEMPLATE_FOLDER_NAME)
CALENDARS_TEMPLATE_FOLDER_NAME = "calendars"
CALENDAR_TEMPLATE_FOLDER = os.path.join(TEMPLATE_FOLDER, CALENDARS_TEMPLATE_FOLDER_NAME)
STATIC_FOLDER_NAME = "static"
STATIC_FOLDER_PATH = os.path.join(HERE, STATIC_FOLDER_NAME)

# specification
PARAM_SPECIFICATION_URL = "specification_url"

# globals
app = Flask(__name__, template_folder="templates")
# Check Configuring Flask-Cache section for more details
cache = Cache(app, config={
    'CACHE_TYPE': 'filesystem',
    'CACHE_DIR': tempfile.mktemp(prefix="cache-")})

def set_JS_headers(response):
    repsonse = make_response(response)
    response.headers['Access-Control-Allow-Origin'] = '*'
    # see https://developer.mozilla.org/en-US/docs/Web/HTTP/CORS/Errors/CORSMissingAllowHeaderFromPreflight
    response.headers['Access-Control-Allow-Headers'] = request.headers.get("Access-Control-Request-Headers")
    response.headers['Content-Type'] = 'text/calendar'
    return response

def set_js_headers(func):
    """Set the response headers for a valid CORS request."""
    def with_js_response(*args, **kw):
        return set_JS_headers(func(*args, **kw))
    return with_js_response


def spec_get(url):
    if url.startswith("/"):
        assert not ".." in url
        with open(STATIC_FOLDER_PATH + url, encoding="UTF-8") as file:
            return file.read()
    return requests.get(url).text

def get_specification():
    """Build the calendar specification."""
    with open(DEFAULT_SPECIFICATION_PATH, encoding="UTF-8") as file:
        specification = json.load(file)
    # get a request parameter, see https://stackoverflow.com/a/11774434
    url = request.args.get(PARAM_SPECIFICATION_URL, None)
    if url:
        url_specification_response = spec_get(url)
        url_specification_json = json.loads(url_specification_response)
        specification.update(url_specification_json)
    specification.update(request.args)
    return specification
    
def date_to_string(date):
    """Convert a date to a string."""
    # use ISO format
    # see https://docs.dhtmlx.com/scheduler/howtostart_nodejs.html#step4implementingcrud
    # see https://docs.python.org/3/library/datetime.html#datetime.datetime.isoformat
    # see https://docs.python.org/3/library/datetime.html#strftime-and-strptime-behavior
    if hasattr(date, "astimezone"):
        date = date.astimezone(datetime.timezone.utc)
    return date.strftime("%Y-%m-%d %H:%M")

def subcomponent_is_ical_event(event):
    """Whether the calendar subcomponent is an event."""
    # TODO: this is an estimation.
    return "DESCRIPTION" in event

def retrieve_calendar(url):
    """Get the calendar entry from a url.
    
    Also unfold the events to past and future.
    see https://dateutil.readthedocs.io/en/stable/rrule.html
    """
    calendar_text = spec_get(url)
    calendars = icalendar.Calendar.from_ical(calendar_text, multiple=True)
    events = []
    for calendar in calendars:
        assert calendar.get("CALSCALE") == "GREGORIAN", "Only Gregorian calendars are supported."
        for calendar_event in calendar.walk():
            if not subcomponent_is_ical_event(calendar_event):
                continue
            start = calendar_event["DTSTART"].dt
            print("START", start, calendar_event.get("SUMMARY", ""))
            end = calendar_event["DTEND"].dt
            event = {
                "start_date": date_to_string(start),
                "end_date": date_to_string(end),
                "text":  calendar_event.get("SUMMARY", "") + "\n\n" + calendar_event.get("DESCRIPTION", ""),
                "location": calendar_event.get("LOCATION", ""),
            }
            events.append(event)
            # does not work, unfolding it manually
            # "rec_type" : calendar_event.get("RRULE:FREQ", ""),
            #pprint(calendar_event)
            rule = calendar_event.get("RRULE")
            if rule:
                today = datetime.datetime.today()
                one_year_ahead = today.replace(year=today.year + 1, tzinfo=start.tzinfo)
                one_year_past = today.replace(year=today.year - 1, tzinfo=start.tzinfo)
                rule = rrulestr(rule.to_ical().decode(), dtstart = one_year_past, cache=True, unfold=True)
                duration = end - start
                for rstart in rule:
                    # use correct time to start
                    # see https://docs.python.org/3/library/datetime.html#datetime.time.replace
                    rstart = rstart.replace(hour=start.hour, minute=start.minute, second=start.second, microsecond=0, tzinfo=start.tzinfo)
                    if rstart > one_year_ahead:
                        break
                    print(rstart)
                    rend = rstart + duration
                    rec_event = event.copy()
                    rec_event["start_date"] = date_to_string(rstart)
                    rec_event["end_date"] = date_to_string(rend)
                    events.append(rec_event)
    return events

def get_events(specification):
    """Return events."""
    urls = specification["url"]
    if isinstance(urls, str):
        urls = [urls]
    assert len(urls) <= MAXIMUM_THREADS, "You can only merge {} urls.".format(MAXIMUM_THREADS)
    all_events = []
    with ThreadPoolExecutor(max_workers=MAXIMUM_THREADS) as e:
        events_list = e.map(retrieve_calendar, urls)
        for events in events_list:
            all_events.extend(events)
    return events

@app.route("/calendar.<type>", methods=['GET', 'OPTIONS']) 
# use query string in cache, see https://stackoverflow.com/a/47181782/1320237
#@cache.cached(timeout=CACHE_TIMEOUT, query_string=True)
def get_calendar(type):
    """Return a calendar."""
    specification = get_specification()
    if type == "spec":
        return jsonify(specification)
    if type == "events.json":
        entries = get_events(specification)
        return jsonify(entries)
    if type == "html":
        template_name = specification["template"]
        all_template_names = os.listdir(CALENDAR_TEMPLATE_FOLDER)
        assert template_name in all_template_names, "Template names must be file names like \"{}\", not \"{}\".".format("\", \"".join(all_template_names), template_name)
        return render_template(os.path.join(CALENDARS_TEMPLATE_FOLDER_NAME, template_name), 
            specification=specification,
            get_events=get_events,
            json=json,
        )
    raise ValueError("Cannot use extension {}. Please see the documentation or report an error.".format(type))

for folder_name in os.listdir(STATIC_FOLDER_PATH):
    folder_path = os.path.join(STATIC_FOLDER_PATH, folder_name)
    if not os.path.isdir(folder_path):
        continue
    @app.route('/' + folder_name + '/<path:path>', endpoint="static/" + folder_name)
    def send_static(path, folder_name=folder_name):
        return send_from_directory('static/' + folder_name, path)

if __name__ == "__main__":
    app.run(debug=DEBUG, host="0.0.0.0", port=PORT)
