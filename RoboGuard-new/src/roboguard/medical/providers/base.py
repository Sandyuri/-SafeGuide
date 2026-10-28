"""Provider contract for replaceable medical scene sources."""

from abc import ABC, abstractmethod

from roboguard.medical.scene import MedicalScene


class MedicalSceneProvider(ABC):
    """Load one trusted and validated medical scene snapshot."""

    @abstractmethod
    def load_scene(self) -> MedicalScene:
        raise NotImplementedError
