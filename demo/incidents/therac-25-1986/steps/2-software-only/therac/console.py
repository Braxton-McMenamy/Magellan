"""Treatment console: the operator enters the beam mode and energy, the setup task reads them."""

import threading


class TreatmentConsole:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.mode = "xray"
        self.energy_mev = 25

    def operator_edit(self, mode: str, energy_mev: int) -> None:
        """Keyboard handler: the operator corrects the prescription."""
        self.mode = mode
        self.energy_mev = energy_mev

    def set_up_beam(self) -> tuple[str, int]:
        """Treatment task: position the bending magnets for what was entered."""
        return self.mode, self.energy_mev

    def start(self, mode: str, energy_mev: int) -> None:
        threading.Thread(target=self.operator_edit, args=(mode, energy_mev)).start()
        threading.Thread(target=self.set_up_beam).start()
