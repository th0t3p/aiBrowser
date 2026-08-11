"""Tests for authenticated session steering in Phase 2 agent task construction."""

from __future__ import annotations

import pytest

from ai_browser.cli import _NO_FORMS_INSTRUCTION, _build_agent_task

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

HOSTNAME = "example.com"

_OLD_DEFAULT_TASK = (
    f"Explore {HOSTNAME} thoroughly. Look for API documentation, "
    f"developer resources, webhook/callback configuration pages, "
    f"and other technical endpoints. Click through navigation menus, "
    f"expand collapsed sections, and follow links that seem likely "
    f"to reveal more of the site's structure."
)
_EXPECTED_DEFAULT_UNAUTH = f"{_OLD_DEFAULT_TASK} {_NO_FORMS_INSTRUCTION}"

# ---------------------------------------------------------------------------
# _build_agent_task
# ---------------------------------------------------------------------------


class TestBuildAgentTask:
    """Unit tests for the pure task-building helper."""

    # -- authenticated=False, agent_task=None: backward compat --------------

    def test_unauthenticated_default_task_is_byte_identical(self):
        """authenticated=False + no custom task must return the old default verbatim."""
        result = _build_agent_task(
            hostname=HOSTNAME,
            agent_task=None,
            authenticated=False,
        )
        assert result == _EXPECTED_DEFAULT_UNAUTH, (
            f"Unauthenticated default task changed!\n"
            f"Expected: {_EXPECTED_DEFAULT_UNAUTH!r}\n"
            f"Got:      {result!r}"
        )

    # -- authenticated=True, agent_task=None: auth steer -------------------

    def test_authenticated_default_contains_auth_steer_language(self):
        result = _build_agent_task(
            hostname=HOSTNAME,
            agent_task=None,
            authenticated=True,
        )
        assert "logged-in-only" in result

    def test_authenticated_default_contains_no_forms_instruction(self):
        result = _build_agent_task(
            hostname=HOSTNAME,
            agent_task=None,
            authenticated=True,
        )
        assert _NO_FORMS_INSTRUCTION in result

    # -- every combination: no-forms guardrail always present --------------

    @pytest.mark.parametrize("authenticated", [False, True])
    @pytest.mark.parametrize("agent_task", [None, "do X"])
    def test_no_forms_instruction_always_present(
        self, authenticated, agent_task
    ):
        result = _build_agent_task(
            hostname=HOSTNAME,
            agent_task=agent_task,
            authenticated=authenticated,
        )
        assert _NO_FORMS_INSTRUCTION in result, (
            f"Missing no-forms guard! authenticated={authenticated}, "
            f"agent_task={agent_task!r}"
        )

    # -- authenticated=True, agent_task="do X": ordering -------------------

    def test_custom_task_with_auth_ordering(self):
        result = _build_agent_task(
            hostname=HOSTNAME,
            agent_task="do X",
            authenticated=True,
        )
        assert "do X" in result
        assert "logged-in-only" in result
        assert _NO_FORMS_INSTRUCTION in result

        # Order: "do X" < auth hint < _no_forms
        idx_custom = result.index("do X")
        idx_auth = result.index("logged-in-only")
        idx_no_forms = result.index(_NO_FORMS_INSTRUCTION)
        assert idx_custom < idx_auth < idx_no_forms, (
            f"Order violation: custom={idx_custom}, "
            f"auth={idx_auth}, no_forms={idx_no_forms}"
        )

    # -- authenticated=False, agent_task="do X": no auth clutter -----------

    def test_custom_task_unauthenticated_no_auth_hint(self):
        result = _build_agent_task(
            hostname=HOSTNAME,
            agent_task="do X",
            authenticated=False,
        )
        assert "do X" in result
        assert _NO_FORMS_INSTRUCTION in result
        assert "logged-in-only" not in result

    # -- hostname interpolation --------------------------------------------

    def test_hostname_interpolated_in_default(self):
        result = _build_agent_task(
            hostname="mysite.io",
            agent_task=None,
            authenticated=False,
        )
        assert "mysite.io" in result

    def test_hostname_interpolated_in_authenticated_default(self):
        result = _build_agent_task(
            hostname="mysite.io",
            agent_task=None,
            authenticated=True,
        )
        assert "mysite.io" in result
        assert "logged-in-only" in result


# ---------------------------------------------------------------------------
# No-hardcoding guard: categories, not paths
# ---------------------------------------------------------------------------


class TestNoHardcodingGuard:
    """Prove the auth-steer task describes categories, not target-specific paths."""

    # Small denylist of tokens that would indicate target-specific hardcoding.
    # Extend this if a target's well-known paths ever sneak in.
    _DENYLIST = [
        "tiktok",
        "/apps",
        "/settings",
        "/admin",
        "/dashboard",
        "github.com",
        "developers.",
    ]

    @pytest.mark.parametrize("token", _DENYLIST)
    def test_auth_task_free_of_target_tokens(self, token):
        """The auth-steer default task must NOT contain any target-specific token."""
        result = _build_agent_task(
            hostname="example.com",
            agent_task=None,
            authenticated=True,
        )
        assert token not in result, (
            f"Target-specific token {token!r} found in authenticated task:\n{result}"
        )


# ---------------------------------------------------------------------------
# authenticated derivation logic
# ---------------------------------------------------------------------------


class TestAuthenticatedDerivation:
    """The boolean ``authenticated`` is derived as::

        authenticated = bool(cookies_file) or (login_authenticated is True)

    in _run_crawl.  These tests verify the logic directly.
    """

    @pytest.mark.parametrize(
        "cookies_file, login_authenticated, expected",
        [
            ("/tmp/cookies.json", None, True),
            ("/tmp/cookies.json", True, True),
            ("/tmp/cookies.json", False, True),  # cookies take precedence
            (None, True, True),
            (None, False, False),
            (None, None, False),
            ("", True, True),         # bool("") is False
            ("", False, False),
            ("", None, False),
        ],
    )
    def test_derivation_formula(
        self, cookies_file, login_authenticated, expected
    ):
        authenticated = bool(cookies_file) or (login_authenticated is True)
        assert authenticated is expected
