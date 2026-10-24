from pathlib import Path

PAGE = Path("src/oneco_os/resources/bridge/index.html")


def _page() -> str:
    return PAGE.read_text(encoding="utf-8")


def test_cockpit_has_persistent_light_and_dark_themes() -> None:
    page = _page()

    assert ':root[data-theme="light"]' in page
    assert "oneco-cockpit-theme" in page
    assert 'data-action="toggle-theme"' in page
    assert "prefers-color-scheme: light" in page
    assert 'meta name="theme-color"' in page


def test_cockpit_uses_accessible_in_app_board_forms() -> None:
    page = _page()

    assert '<dialog id="action-dialog"' in page
    assert 'aria-labelledby="dialog-title"' in page
    assert 'aria-describedby="dialog-description"' in page
    assert 'id="dialog-context"' in page
    assert '<label for="signal-title">' in page
    assert '<label for="signal-summary">' in page
    assert '<label for="rationale">' in page
    assert '<label for="evidence-gap">' in page
    assert "This field cannot be blank." in page
    assert "invalidInput.focus()" in page
    assert "invalidInput.reportValidity()" not in page
    assert "form.checkValidity()" in page
    assert 'aria-describedby="signal-title-error"' in page
    assert 'aria-describedby="evidence-gap-hint evidence-gap-error"' in page
    assert "input.setAttribute('aria-invalid', 'true')" in page
    assert "prompt(" not in page
    assert "alert(" not in page


def test_cockpit_preserves_attention_and_accessibility_at_narrow_widths() -> None:
    page = _page()

    assert 'class="skip-link"' in page
    assert 'aria-labelledby="attention-heading"' in page
    assert 'id="attention-heading" tabindex="-1"' in page
    assert 'aria-label="Company roles and systems"' in page
    assert "prefers-reduced-motion: reduce" in page
    assert 'grid-template-areas: "roles attention" "roles board"' in page
    assert 'grid-template-areas: "attention" "board" "roles"' in page
    assert page.index('class="attention-rail"') < page.index('class="board" id="board"')
    assert page.index('class="board" id="board"') < page.index('class="rail" aria-label="Company roles and systems"')
    assert "scroll-margin-top: 86px" in page
    assert ".attention{display:none}" not in page.replace(" ", "")


def test_cockpit_refresh_is_serialized_and_does_not_repaint_unchanged_state() -> None:
    page = _page()

    assert "let refreshInFlight = false" in page
    assert "if (refreshInFlight || document.getElementById('action-dialog').open)" in page
    assert "pendingHealthRefresh = pendingHealthRefresh || runHealth" in page
    assert "if (signature !== lastSnapshotSignature)" in page
    assert 'id="sync-status" title="Connecting to OneCo"' in page
    assert 'id="sync-announcer" aria-live="polite"' in page
    assert "sync.textContent = 'Synced'" in page
    assert "sync.textContent = 'Offline'" in page
    assert "const reportFailure = !syncFailed || runHealth" in page
    assert "if (reportFailure) showToast" in page
    assert "setInterval(() => refresh(false), 5000)" in page


def test_cockpit_handles_long_content_and_uses_delegated_actions() -> None:
    page = _page()

    assert "overflow-wrap: anywhere" in page
    assert "function disclosure(text, key)" in page
    assert "const rowDetail = (value, key, disclosureThreshold = 130) =>" in page
    assert "rowDetail(task, 'project:' + project.id + ':task', 72)" in page
    assert "data-disclosure-key" in page
    assert "data-focus-key" in page
    assert "-webkit-line-clamp" not in page
    assert "onclick=" not in page
    assert "event.target.closest('button[data-action]')" in page
    assert "const esc = value =>" in page
    assert '<span class="sr-only"> to </span>' in page


def test_cockpit_messages_are_incrementally_reachable() -> None:
    page = _page()

    assert "let messageLimit = MESSAGE_PREVIEW_LIMIT" in page
    assert "messages.slice(0, messageLimit)" in page
    assert "Math.min(total, messageLimit + 13)" in page
    assert "'Show ' + Math.min(13, remaining) + ' More'" in page


def test_cockpit_theme_toggle_has_one_accessible_state_model() -> None:
    page = _page()

    assert "button.setAttribute('aria-label', 'Switch to ' + next + ' mode')" in page
    assert "button.setAttribute('aria-pressed'" not in page


def test_cockpit_marks_disclosures_and_locks_async_actions() -> None:
    page = _page()

    assert 'class="health-chevron" aria-hidden="true"' in page
    assert ".health-panel[open] .health-chevron" in page
    assert "const pendingActions = new Set()" in page
    assert "pendingActions.has(actionKey)" in page
    assert "button.setAttribute('aria-busy', 'true')" in page
    assert 'id="root" translate="no"' in page
    assert 'class="row-id" translate="no"' in page
    assert 'class="attention-object" translate="no"' in page
