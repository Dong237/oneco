"""Browser regression checks for Cockpit forms and asynchronous refresh.

Run: uv run --with playwright python tests/cockpit_release_check.py
Uses an isolated headless Chrome profile and a synthetic bridge, never a company.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

BOOTSTRAP = """
window.__snapshot = {
  company: 'Release verification', root: '/tmp/oneco-verification',
  roles: {}, signals: [], projects: [], releases: [], attention: [], messages: [],
  capabilities: [{capability_name: 'ghostty', state: 'ok'}],
  bets: [{
    bet_id: 'BET-0001', title: 'Verified opportunity', current_revision: 1,
    current_manifest: {revision: 1, composite_digest: 'a'.repeat(64),
                      validation_state: 'passed'}, latest_decision: null
  }]
};
window.__pendingDecisions = [];
window.__pendingSnapshots = [];
window.__holdSnapshot = false;
window.__healthCalls = 0;
const deferredDecision = () => new Promise((resolve, reject) => {
  window.__pendingDecisions.push({resolve, reject});
});
window.pywebview = {api: {
  snapshot: async () => {
    if (window.__holdSnapshot) {
      return new Promise(resolve => window.__pendingSnapshots.push(resolve));
    }
    return structuredClone(window.__snapshot);
  },
  refresh_health: async () => { window.__healthCalls++; return []; },
  decide_bet: deferredDecision, decide_release: deferredDecision,
  add_signal: deferredDecision
}};
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path(tempfile.mkdtemp(prefix="oneco-cockpit-check-")),
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    page_path = Path(__file__).resolve().parents[1] / "src/oneco_os/resources/bridge/index.html"
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    results = {}
    errors = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(chrome), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script(BOOTSTRAP)
            page.goto(page_path.as_uri())
            page.wait_for_load_state("networkidle")
            page.evaluate("window.dispatchEvent(new Event('pywebviewready'))")
            expect(page.locator("#sync-status")).to_have_text("Synced")
            expect(page.get_by_text("Verified opportunity", exact=True)).to_be_visible()
            page.screenshot(
                path=str(args.output_dir / "cockpit-wide.png"), full_page=True,
                animations="disabled",
            )

            page.locator('button[data-action="decide-bet"][data-decision="select"]').click()
            page.locator("#rationale").fill("Select these verified bytes")
            page.locator("#dialog-submit").click()
            page.wait_for_function("window.__pendingDecisions.length === 1")
            page.keyboard.press("Escape")
            page.locator('button[data-action="add-signal"]').click()
            page.evaluate("window.__pendingDecisions.shift().resolve({ok: true})")
            expect(page.locator("#toast")).to_contain_text("BET-0001")
            expect(page.locator("#action-dialog")).to_be_visible()
            expect(page.locator("#dialog-title")).to_have_text("Assign a Signal to the CPO")
            expect(page.locator("#dialog-submit")).to_have_text("Assign Signal")
            expect(page.locator("#dialog-submit")).to_be_enabled()
            expect(page.locator("#dialog-error")).to_be_empty()
            results["cancelled_submission_keeps_new_dialog"] = True

            page.keyboard.press("Escape")
            page.locator('button[data-action="decide-bet"][data-decision="select"]').click()
            page.locator("#rationale").fill("A second decision for rejection handling")
            page.locator("#dialog-submit").click()
            page.wait_for_function("window.__pendingDecisions.length === 1")
            page.keyboard.press("Escape")
            page.locator('button[data-action="add-signal"]').click()
            page.evaluate(
                "window.__pendingDecisions.shift().reject(new Error('expected failure'))"
            )
            expect(page.locator("#toast")).to_contain_text("expected failure")
            expect(page.locator("#dialog-title")).to_have_text("Assign a Signal to the CPO")
            expect(page.locator("#dialog-error")).to_be_empty()
            expect(page.locator("#dialog-submit")).to_be_enabled()
            results["old_failure_does_not_mutate_new_form"] = True

            page.keyboard.press("Escape")
            page.evaluate("window.__holdSnapshot = true; void refresh(false)")
            page.wait_for_function("window.__pendingSnapshots.length === 1")
            page.locator('button[data-action="refresh-health"]').click()
            page.evaluate(
                "window.__holdSnapshot = false;"
                "window.__pendingSnapshots.shift()(structuredClone(window.__snapshot))"
            )
            page.wait_for_function("window.__healthCalls === 1")
            expect(page.locator("#sync-status")).to_have_text("Synced")
            results["manual_health_refresh_survives_polling"] = True

            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.locator(".attention-rail")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(
                path=str(args.output_dir / "cockpit-narrow.png"), full_page=True,
                animations="disabled",
            )
            results["narrow_layout_keeps_attention_without_overflow"] = True
            assert not errors, errors
            results["browser_errors"] = errors
        finally:
            browser.close()
    (args.output_dir / "cockpit-browser-check.json").write_text(
        json.dumps(results, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
