"""Browser-driven UI tests (Playwright) against a real running server -- not the
FastAPI TestClient, an actual HTML/JS/CSS page in a real browser. Covers the
"map-based UI presentation" testing axis: the map renders real zone geometry,
tabs switch, zone selection drives the recommend panel, fleet mode toggles
correctly, and the earnings simulator produces a result."""


def test_map_loads_with_all_zones(page, live_server):
    page.goto(live_server)
    # each zone polygon renders as an SVG path in Leaflet's default renderer
    page.wait_for_selector(".leaflet-interactive", timeout=10_000)
    paths = page.locator(".leaflet-interactive")
    assert paths.count() == 263


def test_legend_and_default_hour_label(page, live_server):
    page.goto(live_server)
    page.wait_for_selector(".leaflet-interactive")
    assert "Hour: 18:00" in page.locator("#hour-label").inner_text()
    assert page.locator("#legend-scale").is_visible()


def test_tab_switching(page, live_server):
    page.goto(live_server)
    page.wait_for_selector(".leaflet-interactive")

    page.click("#tab-earnings")
    assert "active" in page.locator("#view-earnings").get_attribute("class")
    assert "active" not in page.locator("#view-map").get_attribute("class")

    page.click("#tab-metrics")
    assert "active" in page.locator("#view-metrics").get_attribute("class")
    page.wait_for_selector("#stat-tiles .stat-tile")
    assert page.locator("#stat-tiles .stat-tile").count() > 0

    page.click("#tab-map")
    assert "active" in page.locator("#view-map").get_attribute("class")


def test_zone_selection_shows_recommendations(page, live_server):
    page.goto(live_server)
    page.wait_for_selector(".leaflet-interactive")
    # drive selection directly via the page's own JS rather than a brittle pixel
    # click on a specific map polygon
    page.evaluate("selectZone(161)")
    page.wait_for_selector("#selection-panel .reco-list li")
    items = page.locator("#selection-panel .reco-list li")
    assert items.count() == 5
    # heading is styled text-transform: uppercase, so match case-insensitively
    assert "best zones to reposition to" in page.locator("#selection-panel").inner_text().lower()


def test_fleet_mode_toggle(page, live_server):
    page.goto(live_server)
    page.wait_for_selector(".leaflet-interactive")

    page.click("#mode-fleet")
    assert page.locator("#fleet-panel").is_visible()
    assert not page.locator("#selection-panel").is_visible()

    page.click("#mode-single")
    assert page.locator("#selection-panel").is_visible()
    assert not page.locator("#fleet-panel").is_visible()


def test_fleet_balance_produces_assignments(page, live_server):
    page.goto(live_server)
    page.wait_for_selector(".leaflet-interactive")
    page.click("#mode-fleet")
    page.evaluate("toggleFleetDriver(161)")
    page.evaluate("toggleFleetDriver(162)")
    page.click("#fleet-balance-btn")
    page.wait_for_selector("#fleet-results .reco-list li")
    assert page.locator("#fleet-results .reco-list li").count() == 2


def test_earnings_simulator_runs(page, live_server):
    page.goto(live_server)
    page.wait_for_selector(".leaflet-interactive")
    page.click("#tab-earnings")
    # <option> elements are never "visible" per Playwright's actionability rules;
    # wait for them to exist in the DOM instead.
    page.wait_for_selector("#sim-zone-select option", state="attached")
    page.click("#sim-run-btn")
    page.wait_for_selector("#sim-results .stat-tiles .stat-tile", timeout=10_000)
    text = page.locator("#sim-results").inner_text()
    assert "Stay put" in text
    assert "Follow model" in text
