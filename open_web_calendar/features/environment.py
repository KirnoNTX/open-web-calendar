# SPDX-FileCopyrightText: 2024 Nicco Kunzmann and Open Web Calendar Contributors <https://open-web-calendar.quelltext.eu/>
#
# SPDX-License-Identifier: GPL-2.0-only

"""Browser fixture setup and teardown

see https://behave.readthedocs.io/en/stable/practical_tips.html#selenium-example
"""

import contextlib
import copy
import http.server
import multiprocessing
import os
import random
import socketserver
import subprocess
import sys
import threading
import time
from pathlib import Path
from shutil import rmtree
from typing import TYPE_CHECKING

import requests
from behave import fixture, use_fixture
from behave.model import Status, Step
from selenium import webdriver
from selenium.webdriver import Firefox, FirefoxOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.common.service import Service
from selenium.webdriver.firefox.service import Service as FirefoxService
from selenium.webdriver.remote.webdriver import WebDriver
from werkzeug import run_simple

from open_web_calendar.test.rpc import RPCPipe

if TYPE_CHECKING:
    from selenium.webdriver.remote.webelement import WebElement

HERE = Path(__file__).parent.absolute()
PROJECT_FOLDER = HERE.parent
if PROJECT_FOLDER.name == "open_web_calendar":
    PROJECT_FOLDER = PROJECT_FOLDER.parent
sys.path.append(str(PROJECT_FOLDER))

from open_web_calendar.app import DEFAULT_SPECIFICATION, app  # noqa: E402

CALENDAR_FOLDER = HERE / "calendars"
SCREENSHOTS_FOLDER = PROJECT_FOLDER / "screenshots"
SCREENSHOTS_FOLDER.mkdir(parents=True, exist_ok=True)
# timeout in seconds
WAIT = 10

EXPECTED_DOWNLOADS = HERE / "downloads"
ACTUAL_DOWNLOADS = HERE / "downloads_by_tests"


def locate_command(command: str):
    """Locate a command on the command line or return ''."""
    code, output = subprocess.getstatusoutput(f"which {command}")  # noqa: S605
    if code == 0:
        return output.strip()
    return ""


def configure_browser(browser: WebDriver):
    """Make common configurations to the browser."""
    browser.implicitly_wait(WAIT)
    browser.set_page_load_timeout(WAIT)
    browser.switch_to.new_window("tab")


@fixture
def browser_firefox(context):
    # run firefox in headless mode
    # see https://stackoverflow.com/a/47642457/1320237
    options = FirefoxOptions()
    options.add_argument("--headless")
    # Set download directory
    # see https://stackoverflow.com/a/36309735
    options.set_preference("browser.download.folderList", 2)
    options.set_preference("browser.download.dir", str(context.download_directory))
    options.set_preference("browser.download.useDownloadDir", True)
    options.set_preference("browser.download.viewableInternally.enabledTypes", "")
    options.set_preference(
        "browser.helperApps.neverAsk.saveToDisk",
        ";".join(  # noqa: FLY002
            [
                "application/pdf",
                "text/plain",
                "text/calendar",
                "application/text",
                "text/xml",
            ]
        ),
    )
    options.set_preference("pdfjs.disabled", True)  # disable the built-in PDF viewer

    # specify firefox executible and gecko drivers
    # see https://stackoverflow.com/a/76852633
    # see https://stackoverflow.com/a/71766991/1320237
    # specify the path to your geckodriver
    geckodriver_path = Path("/snap/bin/geckodriver")
    # Set the language for the tests
    options.set_preference("intl.accept_languages", "en-US, en")
    # construct the arguments
    kw = {"options": options}
    if geckodriver_path.exists():
        kw["service"] = FirefoxService(executable_path=geckodriver_path)
    browser = Firefox(**kw)
    context.browser = browser
    configure_browser(browser)
    yield context.browser
    # -- CLEANUP-FIXTURE PART:
    context.browser.quit()


@fixture
def browser_chrome(context):
    # see https://behave.readthedocs.io/en/stable/tutorial.html#environmental-controls
    # for an example
    # lots of options come from https://stackoverflow.com/a/52340526/1320237
    options = webdriver.ChromeOptions()
    options.add_argument(
        "--headless"
    )  # from https://stackoverflow.com/q/56637973/1320237
    options.add_argument(
        "start-maximized"
    )  # https://stackoverflow.com/a/26283818/1689770
    options.add_argument(
        "enable-automation"
    )  # https://stackoverflow.com/a/43840128/1689770
    options.add_argument("--no-sandbox")  # https://stackoverflow.com/a/50725918/1689770
    options.add_argument(
        "--disable-dev-shm-usage"
    )  # https://stackoverflow.com/a/50725918/1689770
    options.add_argument(
        "--disable-browser-side-navigation"
    )  # https://stackoverflow.com/a/49123152/1689770
    options.add_argument(
        "--disable-gpu"
    )  # https://stackoverflow.com/questions/51959986/how-to-solve-selenium-chromedriver-timed-out-receiving-message-from-renderer-exc

    # set the download directory
    # from https://www.browserstack.com/guide/download-file-using-selenium-python
    prefs = {"download.default_directory": str(context.download_directory)}
    # example: prefs = {"download.default_directory" : "C:\Tutorial\down"};
    options.add_experimental_option("prefs", prefs)

    # executable_path from https://stackoverflow.com/a/76550727/1320237
    path = locate_command("chromium.chromedriver") or locate_command("chromedriver")
    service = ChromeService(executable_path=path)
    context.browser = webdriver.Chrome(service=service, options=options)
    configure_browser(context.browser)
    yield context.browser
    context.browser.quit()


@fixture
def browser_safari(context):
    context.browser = webdriver.Safari()
    configure_browser(context.browser)
    yield context.browser
    context.browser.quit()


browsers = {
    "firefox": browser_firefox,
    "chrome": browser_chrome,
    "safari": browser_safari,
}


@fixture
def set_window_size(context):
    """Set the window size of the browser."""
    # see https://stackoverflow.com/a/55878622/1320237
    if "window" in context.config.userdata:
        width, height = context.config.userdata["window"].split("x")
        context.browser.set_window_size(int(width), int(height))


def get_free_port(start=10000, end=60000):
    """Return a free port number."""
    return random.randint(start, end)  # noqa: S311


def run_wsgi_server(port: int, pipe: RPCPipe.ProcessingPipe):
    """Run the WSGI server."""
    pipe.start()
    os.environ["APP_DEBUG"] = "true"
    run_simple("localhost", port, app, use_debugger=True)


def app_server(context):
    """Start the flask app in a server."""
    app_port = get_free_port()

    # from https://werkzeug.palletsprojects.com/en/2.1.x/serving/#shutting-down-the-server
    # see also https://stackoverflow.com/questions/72824420/how-to-shutdown-flask-server
    pipe = RPCPipe()
    context.server = pipe
    p = multiprocessing.Process(
        target=run_wsgi_server, args=(app_port, pipe.for_other_process)
    )
    p.start()
    context.index_page = url = f"http://localhost:{app_port}/"

    def terminate():
        with contextlib.suppress(PermissionError):
            p.terminate()  # server is already down

    wait_for_http_server(url, on_error=terminate)
    yield
    terminate()


def wait_for_http_server(url, on_error=lambda: None):
    """Make sure the HTTP server is up and running."""
    print(f"HTTP SERVER: Waiting for {url} to start ... ", end="")
    timeout = time.time() + WAIT
    ty = err = tb = None
    while time.time() < timeout:
        try:
            requests.get(url, timeout=WAIT)
            ty = err = tb = None
            break
        except:
            ty, err, tb = sys.exc_info()
            time.sleep(0.01)
    if err is not None:
        print("FAIL")
        on_error()
        raise err.with_traceback(tb)
    print("OK")


@fixture
def calendars_server(context):
    """Serve the calendar files so they can be requested.

    see https://stackoverflow.com/a/52531444/1320237
    """
    # reuse address
    # see https://zaiste.net/posts/python_simplehttpserver_not_closing_port/
    socketserver.TCPServer.allow_reuse_address = True
    port = 8001
    host = "localhost"
    context.calendars_url = f"http://{host}:{port}/"

    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=CALENDAR_FOLDER, **kwargs)

    try:
        httpd = socketserver.TCPServer((host, port), Handler)
    except OSError as e:
        if e.errno == 98:
            # OSError: [Errno 98] Address already in use
            wait_for_http_server(context.calendars_url)
            yield
            return
        else:
            raise
    # from https://werkzeug.palletsprojects.com/en/2.1.x/serving/#shutting-down-the-server
    # see also https://stackoverflow.com/questions/72824420/how-to-shutdown-flask-server
    t = threading.Thread(target=httpd.serve_forever)
    t.start()

    def final():
        try:
            httpd.server_close()
            httpd.shutdown()
        except PermissionError:
            pass

    wait_for_http_server(context.calendars_url, on_error=final)
    yield
    final()


def downloads(context):
    """Create a download location."""
    context.expected_download_directory = EXPECTED_DOWNLOADS
    context.download_directory = ACTUAL_DOWNLOADS
    rmtree(context.download_directory, ignore_errors=True)
    context.download_directory.mkdir(parents=True, exist_ok=True)


def before_all(context):
    context.after_scenario = []
    use_fixture(downloads, context)
    browser = browsers[context.config.userdata["browser"]]
    use_fixture(browser, context)
    use_fixture(set_window_size, context)
    use_fixture(app_server, context)
    use_fixture(calendars_server, context)


os.environ["OWC_SPECIFICATION"] = str(HERE / "environment_specification.yml")


def before_scenario(context, scenario):
    """Reset the calendar for each scenario.

    Empty url and set the timezone and other parameters.
    """
    context.server.capture_stdout()
    context.specification = copy.deepcopy(DEFAULT_SPECIFICATION)
    context.specification["url"] = []
    context.browser.switch_to.new_window("tab")
    context.current_recording = ""


def after_step(context, step: Step):
    """Run after each step to check success."""
    # see https://stackoverflow.com/a/31545036/1320237
    if step.status == Status.failed:
        # https://pythonspot.com/selenium-take-screenshot/
        browser: WebDriver = context.browser
        # Take screenshot of element
        # see https://stackoverflow.com/a/53825388/1320237
        element: WebElement = browser.find_element(By.TAG_NAME, "body")
        file = SCREENSHOTS_FOLDER / f"{Path(step.filename).stem}@line-{step.line}.png"
        print(f"Test failed, capturing screenshot to {file}")
        element.screenshot(str(file))
        print(context.server.get_output())


def before_step(context, step):
    """Run before each step."""
    # from https://stackoverflow.com/a/73913239
    context.step_name = step.name


def after_scenario(context, _):
    """Run after each scenario."""
    context.browser.close()
    context.browser.switch_to.window(context.browser.window_handles[0])
    while context.after_scenario:
        context.after_scenario.pop()()


# remove this error above
# Error terminating service process.
# Traceback (most recent call last):
#   File "selenium/webdriver/common/service.py", line 181, in _terminate_process
#     self.process.terminate()
#   File "lib/python3.11/subprocess.py", line 2204, in terminate
#     self.send_signal(signal.SIGTERM)
#   File ".11.6/lib/python3.11/subprocess.py", line 2196, in send_signal
#     os.kill(self.pid, sig)
# PermissionError: [Errno 13] Permission denied

_terminate_process = Service._terminate_process  # noqa: SLF001


def _patch(*args, **kw):
    with contextlib.suppress(PermissionError):
        return _terminate_process(*args, **kw)


Service._terminate_process = _patch  # noqa: SLF001
