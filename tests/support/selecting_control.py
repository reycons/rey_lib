"""A Control double that also answers a governed step's own questions.

Copied from the legacy file_operator test support (``SelectingControl``) for the
Loader replacement's tests (row 589): the shared ``ControlDouble`` plus the
selector, run-log and current-classification answers a governed step asks of its
Control.
"""

from __future__ import annotations

from tests.support.control_double import ControlDouble

__all__ = ["SelectingControl"]


class SelectingControl(ControlDouble):
    """The shared double, plus the answers a governed step asks of its Control.

    ``call_rows`` stands in for the installation's selector procedure. Which
    rows a step is handed is configuration, not part of the governed manifest
    surface the shared double reproduces, so it is supplied here rather than
    added there.

    ``write_run_log_record`` mints a run-log identity the way the database does.
    Governed writes are evidence-first and refuse to record anything whose
    supporting run-log record did not commit, so a step under test needs a run
    log that commits.

    ``get_current_classification`` answers from the file's history, which is
    where classification lives: it is a lifecycle event, and the routine of the
    same name computes the current one from the events rather than reading a
    column.
    """

    def __init__(self) -> None:
        super().__init__()
        self.selected: list[dict] = []
        self.run_log_records: list[dict] = []
        # The profile store, as the routines write it: one row per group per
        # representation, carrying the profile object as it was handed over.
        self.data_profiles: list[dict] = []
        self.data_profile_fields: list[dict] = []
        # The linkage the profile call now also writes, in one transaction with
        # the profile: one file type per profile, and which type each file
        # carries. Kept as mappings because that is what they are -- a profile
        # has at most one type, and a file at most one stamp.
        self.file_types: dict[int, int] = {}
        self.file_type_stamps: dict[int, int] = {}
        # Which file types were asked to have their transform definition
        # maintained, and in which mode. The ASK is what a caller is
        # responsible for; what the routine writes is the routine's own
        # contract and is not reproduced here.
        self.transform_maintenance: list[dict] = []

    def data_profile_for_key(self, data_profile_key: str,
                             required: bool = True):
        """The profile this group already has, or None if it has none."""
        for row in self.data_profiles:
            if row["data_profile_key"] == data_profile_key:
                return row["data_profile_id"]
        return None

    def insert_data_profile(self, data_profile_key: str, field_count: int,
                            header_definition: str, row_count: int,
                            file_manifest_id: int,
                            required: bool = True, **values) -> dict:
        """Tie one file to its profile and its type, as the routine does.

        Idempotent by identity, as the routine is: profiling the same group
        again resolves the profile that exists rather than making a second one.

        Matched on the natural key the routine matches on -- field_count,
        data_profile_key and header_definition. installation_id is the fourth
        member and is absent here for the same reason it is absent from
        Control.insert_data_profile: the procedure map supplies it, and this
        double serves one installation.

        row_count is NOT part of the match. It is how many rows one delivery
        carried, so matching on it would make every delivery a new profile --
        which is exactly what it did.

        Matching on the key alone would agree with a database that no longer
        exists. data_profile_identity_uk is four columns precisely so that one
        provider changing a layout creates a second profile rather than
        silently reusing the first.

        THE TYPE AND THE STAMP ARE MODELLED, not skipped. One file type per
        profile, and the file records which one -- so a caller that stops
        passing the file, or that reads only the profile id, fails here rather
        than against the database. Returns both ids under the routine's own
        output names, because that is what the dataset binding hands back.
        """
        identity = (field_count, data_profile_key, header_definition)
        data_profile_id = None
        for row in self.data_profiles:
            if (row.get("field_count"), row["data_profile_key"],
                    row.get("header_definition")) == identity:
                data_profile_id = row["data_profile_id"]
                break
        if data_profile_id is None:
            data_profile_id = len(self.data_profiles) + 1
            self.data_profiles.append({
                "data_profile_id": data_profile_id,
                "data_profile_key": data_profile_key,
                "field_count": field_count,
                "header_definition": header_definition,
                "row_count": row_count,
                # Every value under its own name, as the columns hold them.
                **values,
            })

        # THE LINK: one type per profile, and nothing else on it. The real
        # table's file_type_key, key_fields and signature are left unset here
        # for the same reason the routine leaves them NULL -- this flow
        # establishes a link, not a description.
        file_type_id = self.file_types.get(data_profile_id)
        if file_type_id is None:
            file_type_id = len(self.file_types) + 1
            self.file_types[data_profile_id] = file_type_id

        # THE STAMP, guarded as the routine guards it: a file is never moved
        # from one type to another by profiling it.
        stamped = self.file_type_stamps.get(file_manifest_id)
        if stamped is not None and stamped != file_type_id:
            raise AssertionError(
                f"File {file_manifest_id} already points at file type "
                f"{stamped}, so it cannot be stamped with {file_type_id}."
            )
        self.file_type_stamps[file_manifest_id] = file_type_id

        return {"o_data_profile_id": data_profile_id,
                "o_file_type_id": file_type_id}

    def enrich_data_profile(self, data_profile_id: int,
                            required: bool = True, **values) -> None:
        """Fill what the profile is missing, and overwrite nothing.

        COALESCE, as the routine has it: a value already stored came from a real
        earlier reading of a file with this structure, and replacing it would
        make the stored value mean "the last file profiled" rather than "what
        this structure was measured as". A double that assigned unconditionally
        would pass a test the database answers differently.
        """
        row = next((profile for profile in self.data_profiles
                    if profile["data_profile_id"] == data_profile_id), None)
        if row is None:
            return
        for name, value in values.items():
            if value is not None and row.get(name) is None:
                row[name] = value

    def data_profile_fields_complete(self, data_profile_id: int,
                                     required: bool = True) -> bool:
        """Whether this profile's field rows are all there, as the routine says.

        COUNTED PER REPRESENTATION against the profile's own field_count, not
        tested for presence. A double answering "any rows exist" would agree
        with a database that does not: p_data_profile_field_ins carries
        ON CONFLICT DO NOTHING, so a half-written set is reachable, and the
        whole point of this check is to finish one.

        False for a profile this double has never seen -- a profile that is not
        there certainly has no fields.
        """
        profile = next((row for row in self.data_profiles
                        if row["data_profile_id"] == data_profile_id), None)
        if profile is None:
            return False
        expected = profile.get("field_count")
        if expected is None:
            return False
        counts = {
            representation: sum(
                1 for row in self.data_profile_fields
                if row["data_profile_id"] == data_profile_id
                and row["data_profile_field_type"] == representation
            )
            for representation in ("clear", "redacted")
        }
        return all(count == expected for count in counts.values())

    def insert_data_profile_field(self, data_profile_id: int, field_name: str,
                                  data_profile_field_type: str,
                                  required: bool = True, **values) -> None:
        """One field reading, written only if that reading is not there.

        The unique key reproduced: (data_profile_id, field_name,
        data_profile_field_type). A double that rewrote an existing row would
        pass a test the database answers differently.
        """
        identity = (data_profile_id, field_name, data_profile_field_type)
        if any((row["data_profile_id"], row["field_name"],
                row["data_profile_field_type"]) == identity
               for row in self.data_profile_fields):
            return
        self.data_profile_fields.append({
            "data_profile_id": data_profile_id,
            "field_name": field_name,
            "data_profile_field_type": data_profile_field_type,
            **values,
        })

    def maintain_transform(self, file_type_id: int, mode: str = "ensure",
                           required: bool = True) -> None:
        """Record that the definition was maintained, and nothing more.

        **It deliberately seeds nothing.** control.p_transform_maintain owns
        the population rules, and a double that reproduced them here would be a
        second implementation of them -- the exact thing keeping that work in
        the database avoids. What a caller can be tested on is that it asked,
        for which file type, and in which mode; what the routine then writes is
        the routine's own contract and is proved against the database.

        So this records the call rather than its effect.
        """
        self.transform_maintenance.append({
            "file_type_id": file_type_id,
            "mode": mode,
        })

    def file_profile(self, file_manifest_id: int, representation: str,
                     required: bool = True) -> dict | None:
        """One file's profile document, reached the way the routine reaches it.

        Mirrors control.f_file_profile_get, which reads the stamped link and
        nothing else:

            file_manifest.file_type_id -> file_type.data_profile_id
                                       -> data_profile (+ its field rows)

        so a file that was never stamped, or whose profile has no field rows of
        that representation, answers None exactly as the routine returns no row.
        A double that answered from somewhere else would pass a test the
        database fails.

        No source_hash. control.data_profile_clear_vw and its redacted sibling
        stopped returning it: it names the ONE file the profile was built from,
        and a profile is shared by every file of its structure.
        """
        if representation not in {"clear", "redacted"}:
            raise AssertionError(
                f"A profile is clear or redacted, not {representation!r}"
            )
        file_type_id = self.file_type_stamps.get(file_manifest_id)
        if file_type_id is None:
            return None
        data_profile_id = next(
            (profile_id for profile_id, type_id in self.file_types.items()
             if type_id == file_type_id),
            None,
        )
        if data_profile_id is None:
            return None
        profile = next(
            (row for row in self.data_profiles
             if row["data_profile_id"] == data_profile_id),
            None,
        )
        if profile is None:
            return None

        fields = [
            row for row in self.data_profile_fields
            if row["data_profile_id"] == data_profile_id
            and row["data_profile_field_type"] == representation
        ]
        if not fields:
            return None
        return {"profile": {
            "profile_schema_version": profile.get("profile_schema_version"),
            "profiler": {
                "application": profile.get("profile_method"),
                "application_version": profile.get("profile_method_version"),
            },
            "eligible_population_rows": profile.get("row_count"),
            "header_definition": profile.get("header_definition"),
            "distribution": profile.get("distribution"),
            "columns": [
                {"name": row["field_name"], "type": row.get("detected_type"),
                 "blank_count": row.get("blank_count"),
                 "min_length": row.get("min_length"),
                 "max_length": row.get("max_length"),
                 "min_decimal_places": row.get("min_decimal_places"),
                 "max_decimal_places": row.get("max_decimal_places")}
                for row in fields
            ],
            "samples": [
                {"column": row["field_name"],
                 "min_numeric": row.get("min_numeric"),
                 "max_numeric": row.get("max_numeric"),
                 "min_date": row.get("min_date"), "max_date": row.get("max_date"),
                 "sample_values": row.get("sample_values"),
                 "null_like_values": row.get("null_like_values"),
                 "constant_value": row.get("constant_value")}
                for row in fields
            ],
        }}

    def call_rows(self, binding_name: str, variables: dict | None = None,
                  required: bool = True) -> list[dict]:
        """Whatever this test declared its selector returns."""
        return [dict(row) for row in self.selected]

    def get_current_classification(self, file_manifest_id: int | None = None,
                                   required: bool = True) -> list[dict]:
        """What the file is classified as now, from its classification events.

        The latest event wins: reclassifying appends another rather than
        replacing the first, and the mutations are held oldest-first.

        The ROW SHAPE is the routine's, not the payload's:
        control.f_file_classification_get returns
        (file_manifest_id, classification, base_path, is_classified), so the
        payload sits under ``classification`` rather than being the row. This
        double returned the bare payload until a caller needed a value out of
        it -- the one production consumer only tested the result for truthiness,
        which both shapes satisfy, so the drift never showed.
        """
        current: dict | None = None
        for mutation in self.list_file_mutations(file_manifest_id):
            if mutation.get("record_type") != "source_file_classification":
                continue
            classification = mutation.get("classification")
            if classification:
                current = classification
        if not current:
            return []
        return [{
            "file_manifest_id": file_manifest_id,
            "classification": current,
            "base_path": current.get("base_path"),
            "is_classified": True,
        }]

    def write_run_log_record(self, **fields) -> int:
        """Mint one run-log identity, as the database does."""
        self.run_log_records.append(fields)
        return len(self.run_log_records)
