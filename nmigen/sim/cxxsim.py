import os
from contextlib import contextmanager

from ..hdl import *
from ..hdl.ast import SignalDict
from .._toolchain.yosys import find_yosys
from .._toolchain.cxx import build_cxx
from ..back import cxxrtl
from ._cmds import *
from ._core import *
from ._cxxrtl_ctypes import cxxrtl_library
from ._pycoro import PyCoroProcess


__all__ = ["Settle", "Delay", "Tick", "Passive", "Active", "Simulator"]


class _SignalState:
    def __init__(self, signal, parts):
        self.signal = signal
        self.parts  = parts

    @property
    def curr(self):
        value = 0
        for part in self.parts:
            value |= part.curr
        return value

    @property
    def next(self):
        value = 0
        for part in self.parts:
            value |= part.next
        return value

    def set(self, value):
        for part in self.parts:
            part.next = value


class _SimulatorState:
    def __init__(self, cxxlib, names):
        self.signals  = SignalDict()
        self.slots    = []
        self.triggers = {}

        self.cxxlib = cxxlib
        self.handle = self.cxxlib.create(self.cxxlib.design_create())
        self.names  = names

    def reset(self):
        self.signals = SignalDict()
        self.slots = []
        self.cxxlib.destroy(self.handle)
        self.handle = self.cxxlib.create(self.cxxlib.design_create())

    def get_signal(self, signal):
        if signal in self.signals:
            return self.signals[signal]
        elif signal in self.names:
            raw_name = b" ".join(map(str.encode, self.names[signal][1:]))
            index = len(self.slots)
            self.slots.append(_SignalState(signal, self.cxxlib.get_parts(self.handle, raw_name)))
            self.signals[signal] = index
            return index
        else:
            raise NotImplementedError

    def add_trigger(self, process, signal, *, trigger=None):
        self.triggers[process, self.get_signal(signal)] = trigger

    def remove_trigger(self, process, signal):
        del self.triggers[process, self.get_signal(signal)]

    def eval(self):
        self.cxxlib.eval(self.handle)

    def commit(self):
        for (process, signal_index), trigger in self.triggers.items():
            signal_state = self.slots[signal_index]
            if (trigger is None and signal_state.next != signal_state.curr or
                    trigger is not None and signal_state.next == trigger):
                process.runnable = True
        return bool(self.cxxlib.commit(self.handle))


class Simulator(SimulatorCore):
    def __init__(self, fragment):
        super().__init__(fragment)

        yosys = cxxrtl._find_yosys()
        cxx_source, name_map = cxxrtl.convert_fragment(self._fragment)

        self._build_dir, so_filename = build_cxx(
            cxx_sources={"sim.cc": cxx_source},
            include_dirs=[yosys.data_dir() / "include"],
            macros=["CXXRTL_INCLUDE_CAPI_IMPL", "CXXRTL_INCLUDE_VCD_CAPI_IMPL"],
            output_name="sim"
        )
        cxxlib = cxxrtl_library(os.path.join(self._build_dir.name, so_filename))
        # cxxlib = cxxrtl_library("./sim.so")

        self._state = _SimulatorState(cxxlib, name_map)
        self._vcd_writers = []

    def _add_coroutine_process(self, process, *, default_cmd):
        self._processes.add(PyCoroProcess(self._state, self._timeline,
                                          self._fragment.domains, process,
                                          default_cmd=default_cmd))

    def reset(self):
        self._state.reset()
        self._timeline.reset()
        for process in self._processes:
            process.reset()

    def _real_step(self):
        for process in self._processes:
            if process.runnable:
                process.runnable = False
                process.run()

        while True:
            self._state.eval()
            if not self._state.commit():
                break

        for vcd_writer, vcd_file in self._vcd_writers:
            self._state.cxxlib.vcd_sample(vcd_writer, int(self._timeline.now * 10 ** 10))
            vcd_file.write(self._state.cxxlib.vcd_read(vcd_writer))

    @contextmanager
    def write_vcd(self, vcd_file):
        if self._timeline.now != 0.0:
            raise ValueError("Cannot start writing waveforms after advancing simulation time")
        with open(vcd_file, "wb") as vcd_file:
            try:
                vcd_writer = self._state.cxxlib.vcd_create()
                self._vcd_writers.append((vcd_writer, vcd_file))
                self._state.cxxlib.vcd_timescale(vcd_writer, 100, b"ps")
                self._state.cxxlib.vcd_add_from(vcd_writer, self._state.handle)
                yield
            finally:
                self._vcd_writers.remove((vcd_writer, vcd_file))
                self._state.cxxlib.vcd_destroy(vcd_writer)
