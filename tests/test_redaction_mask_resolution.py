"""What an explicitly redacted column is guaranteed.

``redact_columns`` is an instruction, not a hint. Once a column is named, the
masking path must obscure its values, and that must not depend on profiling
recognising the data: profiling answers *what kind of data is this*, masking
answers *how should this be obscured*, and the two vocabularies are not
required to agree.

The contract asserted here:

    source != replacement
    same source         -> same replacement
    different sources   -> different replacements, within the namespace
    width               -> always preserved
    character class     -> preserved, except where it cannot differ at all
    neither satisfiable -> raise, never alias
"""

from __future__ import annotations

import pytest

from rey_lib.redaction.char_utils import analyze_pattern, generate_replacement
from rey_lib.redaction.masks import KNOWN_MASKS, apply_mask
from rey_lib.redaction.registry import RedactionRegistry
from rey_lib.errors.error_utils import ConfigError

#: Profiling datatypes that reach the masking side but name no mask. These four
#: are the defect: each one used to leave its column entirely in the clear.
UNRESOLVABLE = ["alpha", "alphanumeric", "boolean", "blank"]


class TestAnUnresolvableProposalStillRedacts:
    """A datatype outside the mask vocabulary means generic redaction."""

    @pytest.mark.parametrize("proposed", [*UNRESOLVABLE, "a_datatype_invented_later"])
    def test_the_value_does_not_survive(self, proposed: str) -> None:
        """The source must not be readable in the output, whatever was proposed.

        Args:
            proposed: The mask type profiling put forward for the column.
        """
        registry = RedactionRegistry(["NAME"], mask_types={"NAME": proposed})

        assert registry.redact("NAME", "SMITH") != "SMITH"

    def test_a_resolvable_proposal_still_picks_its_own_mask(self) -> None:
        """Detection is kept where it maps cleanly; only the mismatch is dropped."""
        registry = RedactionRegistry(["AMOUNT"], mask_types={"AMOUNT": "decimal"})

        assert registry.redact("AMOUNT", "1234.56") == apply_mask("decimal", "1234.56", 1)

    def test_apply_mask_refuses_rather_than_returning_its_input(self) -> None:
        """The primitive fails closed, so the leak cannot return via a new caller."""
        with pytest.raises(ConfigError, match="Unknown mask type"):
            apply_mask("alpha", "SMITH", 1)

        assert "alpha" not in KNOWN_MASKS


class TestTheGenericPathObscuresEveryShape:
    """Difference holds for the values that could previously survive it."""

    # Each is its own counter-1 collision: the encoding is right-aligned and
    # pad-filled, so a source that happens to encode itself came back verbatim.
    @pytest.mark.parametrize(
        "value",
        [
            *[chr(code) for code in range(ord("A"), ord("K"))],   # A-J
            *[str(digit) for digit in range(10)],                 # 0-9
            "AB", "ab", "A1",
            "-", "/", "//", "---", "..", "@",                     # separator-only
        ],
    )
    def test_the_first_value_in_a_column_is_never_itself(self, value: str) -> None:
        """Covers the counter-1 collisions by construction, not by example.

        Args:
            value: The first -- and so counter-1 -- value in its column.
        """
        redacted = RedactionRegistry(["c"]).redact("c", value)

        assert redacted != value
        assert len(redacted) == len(value)

    def test_separator_only_values_do_not_collapse_together(self) -> None:
        """Distinct sources keep distinct replacements.

        The irreducible path cannot vary a character class, so a width-only
        replacement would map every one-character separator onto one value.
        """
        registry = RedactionRegistry(["c"])

        redacted = [registry.redact("c", value) for value in ("-", "/", "_")]

        assert len(set(redacted)) == 3

    def test_separators_are_kept_wherever_the_pattern_can_still_differ(self) -> None:
        """Only the separator-only path gives up separator identity.

        A pattern holding an alphanumeric position has something to vary, so it
        keeps its separators exactly where they were -- which is what the
        fixed-width and trailing-space callers depend on.
        """
        redacted = RedactionRegistry(["c"]).redact("c", "123-45-6789")

        assert redacted != "123-45-6789"
        assert [i for i, ch in enumerate(redacted) if ch == "-"] == [3, 6]

    def test_the_same_source_always_gets_the_same_replacement(self) -> None:
        """Determinism within a registry, which the value map already promised."""
        registry = RedactionRegistry(["c"])

        first = registry.redact("c", "SMITH")
        registry.redact("c", "JONES")

        assert registry.redact("c", "SMITH") == first


class TestTheBoundRestsOnTheGenerator:
    """The search bound is measured, not inferred from alphabet size."""

    @pytest.mark.parametrize("value,expected", [("1", "1234567890"), ("B", "BCDEFGHIJA")])
    def test_a_single_slot_enumerates_every_output_within_ten_counters(
        self, value: str, expected: str,
    ) -> None:
        """Ten consecutive counters produce all ten outputs a slot can take.

        This is what makes the search bound sufficient rather than hopeful: it
        is the generator's actual period, so any single-slot collision is
        escapable. Cardinality alone would not establish it.

        Args:
            value: A one-character source giving a single generated slot.
            expected: Every output that slot produces, in counter order.
        """
        pattern = analyze_pattern(value)

        produced = "".join(generate_replacement(pattern, c) for c in range(1, 11))

        assert produced == expected
        assert len(set(produced)) == 10


class TestExhaustionReusesAnAssignedReplacement:
    """When the space is exhausted, an assigned replacement is reused (row 613).

    Uniqueness within a column is not a requirement (user, 2026-10-03). What
    never changes: the replacement keeps the value's width and character type
    and is never the value itself, and the same source keeps its replacement.
    Fresh outputs are used up before any is reused.
    """

    def test_a_stranded_value_reuses_an_assigned_replacement_not_itself(self) -> None:
        """B-J are taken and the only fresh output left, A, is the source itself.

        It is neither handed back nor refused: an assigned replacement is reused.
        """
        registry = RedactionRegistry(["c"])
        assigned = {registry.redact("c", value) for value in "ZYXWVUTSR"}

        replacement = registry.redact("c", "A")

        assert replacement != "A"
        assert replacement in assigned
        assert len(replacement) == 1 and replacement.isupper()

    def test_a_width_1_column_with_more_values_than_outputs_redacts_every_value(
        self,
    ) -> None:
        """Sixteen one-letter sources, ten one-letter outputs: none is refused."""
        registry = RedactionRegistry(["c"])

        emitted = {value: registry.redact("c", value) for value in "abcdefghijklmnop"}

        assert all(source != replacement for source, replacement in emitted.items())
        assert all(len(r) == 1 and r.islower() for r in emitted.values())
        # Reuse happened, which is now allowed.
        assert len(set(emitted.values())) < len(emitted)

    def test_fresh_outputs_are_used_up_before_any_is_reused(self) -> None:
        registry = RedactionRegistry(["c"])

        first_ten = [registry.redact("c", value) for value in "abcdefghij"]

        assert len(set(first_ten)) == len(first_ten)

    def test_the_same_source_keeps_its_replacement_after_reuse_begins(self) -> None:
        registry = RedactionRegistry(["c"])
        first = {value: registry.redact("c", value) for value in "abcdefghijklmnop"}

        again = {value: registry.redact("c", value) for value in "abcdefghijklmnop"}

        assert again == first

    def test_a_two_character_code_column_beyond_its_space_still_redacts(self) -> None:
        """The live case: more distinct 2-letter codes than the encoder has."""
        registry = RedactionRegistry(["asset_code"])
        codes = [a + b for a in "ABCDEFGHIJKLMNOPQRSTUVWXYZ" for b in "ABCDEF"]

        emitted = {code: registry.redact("asset_code", code) for code in codes}

        assert all(source != replacement for source, replacement in emitted.items())
        assert all(len(r) == 2 and r.isupper() for r in emitted.values())
