from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from PyQt6.QtCore import Qt
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtWidgets import QComboBox

from ert.gui.ertnotifier import ErtNotifier
from ert.storage.realization_storage_state import RealizationStorageState

if TYPE_CHECKING:
    from ert.storage import Ensemble

from enum import Enum


class EnsembleSelectorFilter(Enum):
    NONE = 0
    ONLY_UNDEFINED_ENSEMBLES = 1
    ONLY_PARENTS = 2
    ONLY_VALID_EXPERIMENTS = 3


class EnsembleSelector(QComboBox):
    ensemble_populated = Signal()

    def __init__(
        self,
        notifier: ErtNotifier,
        update_ert: bool = True,
        show_only_undefined: bool = False,
        show_only_no_children: bool = False,
        show_only_with_valid_experiment: bool = False,
    ):
        super().__init__()
        self.notifier = notifier

        # If true current ensemble of ert will be change
        self._update_ert = update_ert
        # only show initialized ensembles
        self._show_only_undefined = show_only_undefined
        self._show_only_with_valid_experiment = show_only_with_valid_experiment
        # If True, we filter out any ensembles which have children
        # One use case is if a user wants to rerun because of failures
        # not related to parameterization. We can allow that, but only
        # if the ensemble has not been used in an update, as that would
        # invalidate the result
        self._show_only_parents = show_only_no_children
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)

        # use enum instead -- replace the arguments in constructor
        self._ensemble_selector_filter = EnsembleSelectorFilter.ONLY_UNDEFINED_ENSEMBLES

        self.setEnabled(False)

        if self._update_ert:
            # Update ERT when this combo box is changed
            self.currentIndexChanged.connect(self._on_current_index_changed)

            # Update this combo box when ERT is changed
            notifier.current_ensemble_changed.connect(
                self._on_global_current_ensemble_changed
            )

        notifier.ertChanged.connect(self.populate)
        notifier.storage_changed.connect(self.populate)

        if notifier.is_storage_available:
            self.populate()

    @property
    def selected_ensemble(self) -> Ensemble:
        return self.itemData(self.currentIndex())

    def populate(self) -> None:
        block = self.blockSignals(True)

        self.clear()

        if self._ensemble_list():
            self.setEnabled(True)

        for ensemble in self._ensemble_list():
            self.addItem(
                f"{ensemble.experiment.name} : {ensemble.name}", userData=ensemble
            )

        current_index = self.findData(
            self.notifier.current_ensemble, Qt.ItemDataRole.UserRole
        )

        self.setCurrentIndex(max(current_index, 0))

        self.blockSignals(block)

        self.ensemble_populated.emit()

    def _ensemble_list(self) -> Iterable[Ensemble]:
        ensemble_list = list(self.notifier.storage.ensembles)

        match self._ensemble_selector_filter:
            case EnsembleSelectorFilter.ONLY_PARENTS:  # self._show_only_parents:
                parents = [
                    ens.parent for ens in self.notifier.storage.ensembles if ens.parent
                ]
                ensemble_list = [val for val in ensemble_list if val.id not in parents]
            case (
                EnsembleSelectorFilter.ONLY_VALID_EXPERIMENTS
            ):  # self._show_only_with_valid_experiment:
                ensemble_list = [
                    ens for ens in ensemble_list if ens.experiment.is_valid()
                ]
            case (
                EnsembleSelectorFilter.ONLY_UNDEFINED_ENSEMBLES
            ):  # self._show_only_undefined:
                ensembles = (
                    ensemble
                    for ensemble in self.notifier.storage.ensembles
                    if all(
                        RealizationStorageState.UNDEFINED in e
                        for e in ensemble.get_ensemble_state()
                    )
                )
                ensemble_list = list(ensembles)

        return sorted(ensemble_list, key=lambda x: x.started_at, reverse=True)

    def _on_current_index_changed(self, index: int) -> None:
        self.notifier.set_current_ensemble(self.itemData(index))

    def _on_global_current_ensemble_changed(self, data: Ensemble | None) -> None:
        self.setCurrentIndex(max(self.findData(data, Qt.ItemDataRole.UserRole), 0))
