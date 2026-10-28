"""The capabilities answered against the canonical object one application hosts (#1196 D1).

A hosting application -- file_operator serving a File -- holds one live
canonical object. ``HostedCapabilities`` is what that application answers an
``invoke`` command with: it checks the invocation against the application's
declarations, then calls the owner's own catalogue, which reaches only the
object the application already holds.

**Three roles, one of them authority.** The registration states what the
application provides; the governed row (code.application_capability, read
through ``Control.get_application_capabilities``) states what is declared for
it; the catalogue is the owner's code saying HOW a key reaches public behavior
of the held object -- capability key -> callable over validated inputs, and
nothing more. A key is invocable only where all three are present and the
registration and governed row agree on every declared field. The catalogue is
not a fourth declaration: it carries no kinds, access, contract or description.

**What it never does.** It receives no transport, token, caller or identity and
looks none up; it checks no caller grant (D2), reads no AI state, and leaves the
object's own refusals to the object. In D1 it answers read capabilities only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Mapping

from rey_lib.config.operation_contract import contract_violations
from rey_lib.errors.error_utils import AppError
from rey_lib.logs import AppLogger

if TYPE_CHECKING:
    from rey_lib.config.applications import Application, ApplicationCapability

__all__ = ["HostedCapabilities"]

_LOG = AppLogger(__name__)

#: The access D1 answers. A write capability is refused until mutation is ruled
#: (#45 step 17); keeping access in the declaration does not authorize it.
_ANSWERED_ACCESS = "read"


class HostedCapabilities:
    """The capabilities one hosting application answers against the object it holds.

    Built once, when the application starts serving, and asked with
    :meth:`invoke` for each ``invoke`` command that arrives on its existing
    channel.
    """

    def __init__(
        self,
        application: Application,
        control: Any,
        catalogue: Mapping[str, Callable[[Mapping[str, Any]], Any]],
        hosted_kind: str,
    ) -> None:
        """Settle which keys are invocable, fail closed per key.

        Args:
            application: The hosting application; its ``capabilities`` are the
                registration's declarations.
            control: The application's Control, the governed read boundary.
            catalogue: The owner's catalogue -- capability key -> a callable of
                validated inputs over the object the application holds.
            hosted_kind: The kind of canonical object the application exposes --
                its opening identity kind where it holds one object (a File, a
                Workflow), the held objects' own kind where it holds several (an
                AI session's AITasks).

        A disagreement leaves that key not invocable and is logged once; the
        application still hosts and presents its object. A failed governed read
        leaves no key invocable.
        """
        self._hosted_kind = str(hosted_kind)
        self._catalogue = dict(catalogue)
        self._invocable: dict[str, ApplicationCapability] = {}
        self._refused: dict[str, str] = {}
        published = {capability.key: capability for capability in application.capabilities}
        try:
            governed = {
                str(row.get("capability_key") or ""): row
                for row in control.get_application_capabilities(application.name)
            }
        except AppError as exc:
            exc.report(
                f"reading the governed capabilities of '{application.name}'; none are invocable",
                source=__name__,
            )
            self._unavailable = (
                f"the governed capabilities of '{application.name}' could not be read"
            )
            return
        self._unavailable = ""
        for key in sorted(set(published) | set(governed) | set(self._catalogue)):
            reason = _disagreement(published.get(key), governed.get(key),
                                   key in self._catalogue)
            if reason:
                self._refused[key] = reason
                _LOG.warning("capability '%s' of '%s' is not invocable: %s",
                             key, application.name, reason)
            else:
                self._invocable[key] = published[key]

    def invoke(self, capability: str, inputs: Any) -> Any:
        """Answer one invocation against the hosted object, or refuse it.

        Checked in order: the key is invocable; the inputs satisfy its input
        contract; the hosted kind is one of its target kinds; its access is
        answered. Then the catalogue's callable is called with the inputs.

        Args:
            capability: The capability key, e.g. ``file.text``.
            inputs: The invocation's inputs, checked against the input contract.

        Returns:
            Whatever the catalogue's callable returns.

        Raises:
            AppError: The invocation is refused, naming why.
        """
        key = str(capability or "")
        if self._unavailable:
            raise AppError(f"capability '{key}' is not invocable: {self._unavailable}.")
        declared = self._invocable.get(key)
        if declared is None:
            reason = self._refused.get(key, "it is not a capability of this application")
            raise AppError(f"capability '{key}' is not invocable: {reason}.")
        violations = contract_violations(declared.input_contract, inputs)
        if violations:
            raise AppError(
                f"capability '{key}' refuses its inputs: " + "; ".join(violations)
            )
        if self._hosted_kind not in declared.target_kinds:
            raise AppError(
                f"capability '{key}' does not apply to the hosted kind "
                f"'{self._hosted_kind}' (target kinds: {', '.join(declared.target_kinds)})."
            )
        if declared.access != _ANSWERED_ACCESS:
            raise AppError(
                f"capability '{key}' declares {declared.access} access; only read "
                "capabilities are answered."
            )
        _LOG.debug("invoking capability '%s' on the hosted %s", key, self._hosted_kind)
        return self._catalogue[key](inputs)


def _disagreement(
    published: ApplicationCapability | None,
    governed: Mapping[str, Any] | None,
    implemented: bool,
) -> str:
    """Why a key is not invocable, or empty when its three sources agree."""
    if published is None:
        return "the registration does not publish it"
    if governed is None:
        return "it is not governed for this application"
    if not implemented:
        return "the application's catalogue does not implement it"
    differing = [
        name for name, mine, theirs in (
            ("description", published.description, str(governed.get("description") or "")),
            ("target_kinds", sorted(published.target_kinds),
             sorted(str(kind) for kind in (governed.get("target_kinds") or []))),
            ("input_contract", dict(published.input_contract),
             governed.get("input_contract")),
            ("access", published.access, str(governed.get("access") or "")),
        )
        if mine != theirs
    ]
    if differing:
        return ("the registration and the governed declaration disagree on "
                + ", ".join(differing))
    return ""
