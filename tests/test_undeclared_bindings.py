"""Bindings the code calls that an installation's maps do not declare.

The failure this answers happened in front of a user: a binding was added to one
installation's procedure map and not the others, and the first launch of the
installation that had been missed stopped with "binding 'resolve_installation'
not found in routine_bindings of map 'control'" -- at the point the run is
created, which is before the run log exists to record it.

Sharper since rey_lib@ccaf1d0: start_batch is called by every Control
construction, so a map missing THAT binding fails at the launch boundary rather
than degrading one call.

These work over inspect_bridge-shaped dicts. The subject is the comparison, and
the comparison needs no estate and no database.
"""

from __future__ import annotations

from rey_lib.repository_map.bridge_index import undeclared_bindings


def _observation(binding: str, resolution: str = "exact", line: int = 10) -> dict:
    """One code observation, as _seam_observations emits it."""
    return {
        "repository_key": "rey_lib",
        "relative_path": "rey_lib/control/control.py",
        "qualified_name": f"Control.{binding}",
        "source_line": line,
        "seam": "control_dispatch",
        "observed_map": "",
        "observed_binding": binding,
        "resolution": resolution,
        "dispatch_method": "_call",
    }


def _inspected(code: list[dict], declared: list[str]) -> dict:
    """What inspect_bridge returns, for ONE installation."""
    return {
        "code": code,
        "bindings": [{"installation": "admin", "map_name": "control",
                      "binding_name": name, "target_kind": "routine"}
                     for name in declared],
        "binding_parameters": [],
        "coverage": [],
    }


class TestABindingTheMapDoesNotDeclare:
    """The partial rollout, caught before the launch rather than by it."""

    def test_a_called_binding_that_is_not_declared_is_returned(self) -> None:
        """The whole point, and it carries the call site as well as the name.

        The original failure named only the binding and left the caller to be
        found by hand.
        """
        gaps = undeclared_bindings(
            _inspected([_observation("start_batch", line=623)], ["end_batch"])
        )

        assert [g["observed_binding"] for g in gaps] == ["start_batch"]
        assert gaps[0]["source_line"] == 623
        assert gaps[0]["qualified_name"] == "Control.start_batch"

    def test_a_declared_binding_is_not_returned(self) -> None:
        """A complete map is silent."""
        assert undeclared_bindings(
            _inspected([_observation("start_batch")], ["start_batch"])
        ) == []

    def test_every_gap_is_returned_not_only_the_first(self) -> None:
        """Reporting one of two understates what has to be rolled out."""
        gaps = undeclared_bindings(
            _inspected(
                [_observation("enrich_data_profile"), _observation("find_files")],
                ["start_batch"],
            )
        )

        assert sorted(g["observed_binding"] for g in gaps) == [
            "enrich_data_profile", "find_files"
        ]


class TestWhatItMustNotReport:
    """Two absences that are not faults, and would bury the ones that are."""

    def test_an_unresolved_dispatch_is_not_a_gap(self) -> None:
        """A configured binding name is not evidence of anything.

        The seam reads literal first arguments, so control.call_rows(procedure,
        ...) is recorded unresolved. Treating that as undeclared would report
        every configuration-driven call in the estate as a fault.
        """
        assert undeclared_bindings(
            _inspected([_observation("procedure", resolution="unresolved")], [])
        ) == []

    def test_a_declared_binding_nothing_calls_is_not_a_gap(self) -> None:
        """The other direction is not this function's question.

        A map may declare more than one seam observes -- eight of local's do --
        because the seam reads one file and only literal arguments.
        """
        assert undeclared_bindings(_inspected([], ["never_called"])) == []


class TestTheShapeItIsGiven:
    """One installation's inspection, and nothing missing from it."""

    def test_one_inspection_holds_one_installation(self) -> None:
        """No installation key is needed, and none is consulted.

        inspect_bridge is called with one loaded ctx, so its bindings are that
        installation's alone. A function taking an installation argument here
        would imply it could be given two, and comparing code against the union
        of several maps is how a gap in one hides behind another.
        """
        gaps = undeclared_bindings(
            _inspected([_observation("find_files")], ["start_batch"])
        )

        assert len(gaps) == 1
        # The binding rows carry an installation; the answer does not depend on
        # it, and the observation -- a code fact -- has none to carry.
        assert "installation" not in gaps[0]

    def test_an_empty_inspection_is_not_an_error(self) -> None:
        """Absent keys answer empty rather than raising.

        A caller holding a partial inspection gets "nothing found", which is
        true of what it holds.
        """
        assert undeclared_bindings({}) == []
        assert undeclared_bindings({"code": [], "bindings": []}) == []
