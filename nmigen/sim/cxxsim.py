from ..hdl import *
from ..hdl.ast import SignalDict
from ._cmds import *
from ._core import *


__all__ = ["Settle", "Delay", "Tick", "Passive", "Active", "Simulator"]


class _SignalState:
    def __init__(self, signal):
        raise NotImplementedError

    @property
    def curr(self):
        raise NotImplementedError

    @property
    def next(self):
        raise NotImplementedError

    def set(self, value):
        raise NotImplementedError


class _SimulatorState:
    def __init__(self):
        self.signals = SignalDict()
        self.slots   = []

    def reset(self):
        raise NotImplementedError

    def get_signal(self, signal):
        raise NotImplementedError

    def add_trigger(self, process, signal, *, trigger=None):
        raise NotImplementedError

    def remove_trigger(self, process, signal):
        raise NotImplementedError

    def commit(self):
        raise NotImplementedError


class Simulator(SimulatorCore):
    def __init__(self, fragment):
        super().__init__(fragment)
        self._state = _SimulatorState()

    def _add_coroutine_process(self, process, *, default_cmd):
        self._processes.add(PyCoroProcess(self._state, self._timeline,
                                          self._fragment.domains, process,
                                          default_cmd=default_cmd))

    def reset(self):
        self._state.reset()
        for process in self._processes:
            process.reset()

    def _real_step(self):
        raise NotImplementedError
