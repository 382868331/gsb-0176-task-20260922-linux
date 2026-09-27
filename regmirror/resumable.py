"""Resumable scans and a publisher that never mixes versions.

Two additions on top of the one-shot :class:`~regmirror.scanner.Scanner`:

* :class:`ScanSession` -- a scan that can be paused between groups and
  resumed later.  All pauses and device changes are driven by the local
  script (no real device, no threads).  Before each step the session
  re-reads the version registers of already committed groups; a group
  whose version moved (or reads odd) is discarded and rescanned, so the
  published snapshot never mixes pre- and post-pause versions.
* :class:`SnapshotPublisher` -- holds the last *complete* snapshot.
  ``update()`` runs a full session; if the retry budget is exhausted
  (:class:`InconsistentScanError`) it keeps the previous complete
  snapshot and reports ``updated=False`` -- an update either replaces
  the whole image or changes nothing.

Budget contract: within one session each group is scanned at most
``ScanConfig.max_attempts`` times in total (first scan plus rescans
after staleness).  Beyond that the session raises
:class:`InconsistentScanError`, so a device that keeps mutating during
every pause cannot make the session run forever.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .errors import ConfigError, InconsistentRead, InconsistentScanError
from .scanner import GroupSnapshot, RegisterGroup, Scanner, Snapshot

__all__ = ["ScanSession", "SnapshotPublisher", "UpdateResult"]


class ScanSession:
    """A resumable, all-or-nothing scan driven one group at a time.

    The caller drives the session with :meth:`step` (or :meth:`run`) and
    may pause arbitrarily between steps.  Each step first revalidates
    the already committed groups against the live version registers,
    discarding any group the device touched during the pause, then
    scans one pending group.  The session finishes only when every
    group is committed and a final revalidation pass finds all
    committed versions still current.
    """

    def __init__(
        self,
        scanner: Scanner,
        device: object,
        groups: Sequence[RegisterGroup],
    ):
        if not isinstance(scanner, Scanner):
            raise ConfigError(
                f"scanner must be a Scanner, got {type(scanner).__name__}"
            )
        self._scanner = scanner
        self._device = device
        # Config errors are raised before any device read, same as scan().
        self._groups = scanner.validate(device, groups)
        self._by_name = {g.name: g for g in self._groups}
        self._pending = [g.name for g in self._groups]
        self._committed: list[GroupSnapshot] = []
        self._scan_counts = {g.name: 0 for g in self._groups}
        self._stale_reasons: dict[str, InconsistentRead] = {}
        self._done = False

    @property
    def done(self) -> bool:
        """True once a complete, revalidated snapshot is available."""
        return self._done

    @property
    def pending_names(self) -> tuple[str, ...]:
        return tuple(self._pending)

    @property
    def committed_names(self) -> tuple[str, ...]:
        return tuple(g.name for g in self._committed)

    def step(self) -> bool:
        """Advance the scan by one group; return True when finished.

        Between calls the caller may pause; any device change during
        the pause is caught by the revalidation at the start of the
        next step.
        """
        if self._done:
            return True
        self._revalidate_committed()
        if not self._pending:
            self._done = True
            return True
        name = self._pending.pop(0)
        group = self._by_name[name]
        scans = self._scan_counts[name] + 1
        if scans > self._scanner.config.max_attempts:
            reason = self._stale_reasons.get(
                name, InconsistentRead(name, -1, -1)
            )
            raise InconsistentScanError(
                self._scanner.config.max_attempts, (reason,)
            )
        self._scan_counts[name] = scans
        snap = self._scanner.scan_group(self._device, group)
        self._committed.append(snap)
        return False

    def run(self) -> Snapshot:
        """Step to completion and return the complete snapshot."""
        while not self.step():
            pass
        return Snapshot(tuple(self._committed))

    def _revalidate_committed(self) -> None:
        """Discard committed groups whose version no longer matches.

        A group is still trustworthy iff its version register reads the
        same even word it was committed under.  Otherwise it goes back
        to the pending queue and is rescanned from scratch.
        """
        still: list[GroupSnapshot] = []
        for snap in self._committed:
            group = self._by_name[snap.name]
            word = self._scanner.read_words(
                self._device, group.version_address, 1
            )[0]
            if word == snap.version and not (word & 1):
                still.append(snap)
            else:
                self._stale_reasons[snap.name] = InconsistentRead(
                    snap.name, snap.version, word
                )
                self._pending.append(snap.name)
        self._committed = still


@dataclass(frozen=True)
class UpdateResult:
    """Outcome of one :class:`SnapshotPublisher` update.

    ``snapshot`` is always the last *complete* snapshot (the new one
    when ``updated`` is True, otherwise the previous one, or None if no
    update ever succeeded).  ``error`` is the InconsistentScanError
    that prevented this update, else None.
    """

    updated: bool
    snapshot: Snapshot | None
    error: InconsistentScanError | None


class SnapshotPublisher:
    """Publishes only complete, version-consistent snapshots.

    Keeps the last successfully scanned snapshot.  ``update()`` either
    replaces it wholesale with a fresh complete snapshot or -- when the
    retry budget is exhausted -- keeps the previous one and reports
    ``updated=False``.  Old and new versions are never mixed: a
    snapshot is published only if every configured group committed in
    the same session.
    """

    def __init__(
        self,
        scanner: Scanner,
        device: object,
        groups: Sequence[RegisterGroup],
    ):
        if not isinstance(scanner, Scanner):
            raise ConfigError(
                f"scanner must be a Scanner, got {type(scanner).__name__}"
            )
        self._scanner = scanner
        self._device = device
        # Fail fast on bad configuration, before any update is attempted.
        self._groups = scanner.validate(device, groups)
        self._snapshot: Snapshot | None = None

    @property
    def snapshot(self) -> Snapshot | None:
        """The last complete published snapshot (None before the first)."""
        return self._snapshot

    def new_session(self) -> ScanSession:
        """A fresh resumable session over the configured device/groups."""
        return ScanSession(self._scanner, self._device, self._groups)

    def publish(self, snapshot: Snapshot) -> UpdateResult:
        """Publish a complete snapshot obtained from a driven session."""
        if not isinstance(snapshot, Snapshot):
            raise ConfigError(
                f"snapshot must be a Snapshot, got {type(snapshot).__name__}"
            )
        expected = {g.name for g in self._groups}
        got = {g.name for g in snapshot.groups}
        if got != expected:
            raise ConfigError(
                f"snapshot groups {sorted(got)} do not match the configured "
                f"groups {sorted(expected)}; refusing to publish a partial "
                "or foreign snapshot"
            )
        self._snapshot = snapshot
        return UpdateResult(True, snapshot, None)

    def update(self) -> UpdateResult:
        """Run one full scan session and publish it, or keep the old one.

        Only :class:`InconsistentScanError` (retry budget exhausted) is
        converted into ``updated=False``; configuration and device
        contract errors propagate unchanged.
        """
        try:
            snapshot = self.new_session().run()
        except InconsistentScanError as exc:
            return UpdateResult(False, self._snapshot, exc)
        return self.publish(snapshot)
