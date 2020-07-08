import inspect

from .._utils import deprecated
from ..hdl import *
from ._cmds import *


__all__ = ["Process", "Timeline", "SimulatorCore"]


class Process:
    def __init__(self, *, is_comb):
        self.is_comb  = is_comb

        self.reset()

    def reset(self):
        self.runnable = self.is_comb
        self.passive  = True

    def run(self):
        raise NotImplementedError


class Timeline:
    def __init__(self):
        self.now = 0.0
        self.deadlines = dict()

    def reset(self):
        self.now = 0.0
        self.deadlines.clear()

    def at(self, run_at, process):
        assert process not in self.deadlines
        self.deadlines[process] = run_at

    def delay(self, delay_by, process):
        if delay_by is None:
            run_at = self.now
        else:
            run_at = self.now + delay_by
        self.at(run_at, process)

    def advance(self):
        nearest_processes = set()
        nearest_deadline = None
        for process, deadline in self.deadlines.items():
            if deadline is None:
                if nearest_deadline is not None:
                    nearest_processes.clear()
                nearest_processes.add(process)
                nearest_deadline = self.now
                break
            elif nearest_deadline is None or deadline <= nearest_deadline:
                assert deadline >= self.now
                if nearest_deadline is not None and deadline < nearest_deadline:
                    nearest_processes.clear()
                nearest_processes.add(process)
                nearest_deadline = deadline

        if not nearest_processes:
            return False

        for process in nearest_processes:
            process.runnable = True
            del self.deadlines[process]
        self.now = nearest_deadline

        return True


class SimulatorCore:
    def __init__(self, fragment):
        self._timeline  = Timeline()
        self._fragment  = Fragment.get(fragment, platform=None).prepare()
        self._processes = set()
        self._clocked   = set()

    def _check_process(self, process):
        if not (inspect.isgeneratorfunction(process) or inspect.iscoroutinefunction(process)):
            raise TypeError("Cannot add a process {!r} because it is not a generator function"
                            .format(process))

    def _add_coroutine_process(self, process, *, default_cmd):
        raise NotImplementedError

    def add_process(self, process):
        self._check_process(process)
        def wrapper():
            # Only start a bench process after comb settling, so that the reset values are correct.
            yield Settle()
            yield from process()
        self._add_coroutine_process(wrapper, default_cmd=None)

    def add_sync_process(self, process, *, domain="sync"):
        self._check_process(process)
        def wrapper():
            # Only start a sync process after the first clock edge (or reset edge, if the domain
            # uses an asynchronous reset). This matches the behavior of synchronous FFs.
            yield Tick(domain)
            yield from process()
        return self._add_coroutine_process(wrapper, default_cmd=Tick(domain))

    def add_clock(self, period, *, phase=None, domain="sync", if_exists=False):
        """Add a clock process.

        Adds a process that drives the clock signal of ``domain`` at a 50% duty cycle.

        Arguments
        ---------
        period : float
            Clock period. The process will toggle the ``domain`` clock signal every ``period / 2``
            seconds.
        phase : None or float
            Clock phase. The process will wait ``phase`` seconds before the first clock transition.
            If not specified, defaults to ``period / 2``.
        domain : str or ClockDomain
            Driven clock domain. If specified as a string, the domain with that name is looked up
            in the root fragment of the simulation.
        if_exists : bool
            If ``False`` (the default), raise an error if the driven domain is specified as
            a string and the root fragment does not have such a domain. If ``True``, do nothing
            in this case.
        """
        if isinstance(domain, ClockDomain):
            pass
        elif domain in self._fragment.domains:
            domain = self._fragment.domains[domain]
        elif if_exists:
            return
        else:
            raise ValueError("Domain {!r} is not present in simulation"
                             .format(domain))
        if domain in self._clocked:
            raise ValueError("Domain {!r} already has a clock driving it"
                             .format(domain.name))

        half_period = period / 2
        if phase is None:
            # By default, delay the first edge by half period. This causes any synchronous activity
            # to happen at a non-zero time, distinguishing it from the reset values in the waveform
            # viewer.
            phase = half_period
        def clk_process():
            yield Passive()
            yield Delay(phase)
            # Behave correctly if the process is added after the clock signal is manipulated, or if
            # its reset state is high.
            initial = (yield domain.clk)
            steps = (
                domain.clk.eq(~initial),
                Delay(half_period),
                domain.clk.eq(initial),
                Delay(half_period),
            )
            while True:
                yield from iter(steps)
        self._add_coroutine_process(clk_process, default_cmd=None)
        self._clocked.add(domain)

    def reset(self):
        """Reset the simulation.

        Assign the reset value to every signal in the simulation, and restart every user process.
        """
        raise NotImplementedError

    def _real_step(self):
        """Step the simulation.

        Run every process and commit changes until a fixed point is reached. If there is
        an unstable combinatorial loop, this function will never return.
        """
        raise NotImplementedError

    # TODO(nmigen-0.4): replace with _real_step
    @deprecated("instead of `sim.step()`, use `sim.advance()`")
    def step(self):
        return self.advance()

    def advance(self):
        """Advance the simulation.

        Run every process and commit changes until a fixed point is reached, then advance time
        to the closest deadline (if any). If there is an unstable combinatorial loop,
        this function will never return.

        Returns ``True`` if there are any active processes, ``False`` otherwise.
        """
        self._real_step()
        self._timeline.advance()
        return any(not process.passive for process in self._processes)

    def run(self):
        """Run the simulation while any processes are active.

        Processes added with :meth:`add_process` and :meth:`add_sync_process` are initially active,
        and may change their status using the ``yield Passive()`` and ``yield Active()`` commands.
        Processes compiled from HDL and added with :meth:`add_clock` are always passive.
        """
        while self.advance():
            pass

    def run_until(self, deadline, *, run_passive=False):
        """Run the simulation until it advances to ``deadline``.

        If ``run_passive`` is ``False``, the simulation also stops when there are no active
        processes, similar to :meth:`run`. Otherwise, the simulation will stop only after it
        advances to or past ``deadline``.

        If the simulation stops advancing, this function will never return.
        """
        assert self._timeline.now <= deadline
        while (self.advance() or run_passive) and self._timeline.now < deadline:
            pass
