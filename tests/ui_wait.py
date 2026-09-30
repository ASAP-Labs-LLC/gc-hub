"""Explicit waits for the headless-Chrome tests (not a test module).

``wait_for(drv, pred, timeout)`` is Selenium's ``WebDriverWait`` on a
predicate: it polls until ``pred()`` is truthy and returns that value, and
treats a stale element, a missing element or a script error (the page still
rendering: the lists are rebuilt on every refresh) as "not yet". On timeout it
returns ``pred()``'s last answer (falsy) so the caller's ``assert`` can say
what it saw. ``click_when_ready`` finds and clicks in one step, retried, so a
list re-rendered between the find and the click can't make it flaky.
"""
from __future__ import annotations

from selenium.common.exceptions import (JavascriptException, NoSuchElementException,
                                        StaleElementReferenceException, TimeoutException,
                                        ElementClickInterceptedException,
                                        ElementNotInteractableException)
from selenium.webdriver.support.ui import WebDriverWait

NOT_YET = (JavascriptException, NoSuchElementException, StaleElementReferenceException,
           ElementClickInterceptedException, ElementNotInteractableException)


def wait_for(drv, pred, timeout=20.0):
    try:
        return WebDriverWait(drv, timeout, poll_frequency=0.1,
                             ignored_exceptions=NOT_YET).until(lambda _d: pred())
    except TimeoutException:
        try:
            return pred()
        except NOT_YET:
            return None


def click_when_ready(drv, css, timeout=20.0, *, context=False):
    """Find ``css`` and click it (``context``: right-click), retrying while
    the element is missing, stale or covered. Returns True once clicked."""
    from selenium.webdriver import ActionChains

    def attempt():
        el = drv.find_element("css selector", css)
        if context:
            ActionChains(drv).context_click(el).perform()
        else:
            el.click()
        return True

    return wait_for(drv, attempt, timeout)
