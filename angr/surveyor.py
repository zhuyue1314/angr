#!/usr/bin/env python

import multiprocessing
import concurrent.futures

from .path import Path

import logging
l = logging.getLogger("angr.Surveyor")

STOP_RUNS = False
PAUSE_RUNS = False

def enable_singlestep():
    global PAUSE_RUNS
    PAUSE_RUNS = True
def disable_singlestep():
    global PAUSE_RUNS
    PAUSE_RUNS = False
def stop_analyses():
    global STOP_RUNS
    STOP_RUNS = True
def resume_analyses():
    global STOP_RUNS
    STOP_RUNS = False

import signal
def handler(signum, frame): # pylint: disable=W0613,
    if signum == signal.SIGUSR1:
        stop_analyses()
    elif signum == signal.SIGUSR2:
        enable_singlestep()

signal.signal(signal.SIGUSR1, handler)
signal.signal(signal.SIGUSR2, handler)

class Surveyor(object):
    '''
    The surveyor class eases the implementation of symbolic analyses. This
    provides a base upon which analyses can be implemented. It has the
    following overloadable functions/properties:

        done: returns True if the analysis is done (by default, this is when
              self.active is empty)
        run: runs a loop of tick()ing and spill()ming until self.done is
             True
        tick: ticks all paths forward. The default implementation calls
              tick_path() on every path
        tick_path: moves a provided path forward, returning a set of new
                   paths
        spill: spills all paths, in-place. The default implementation first
              calls spill_path() on every path, then spill_paths() on the
              resulting sequence, then keeps the rest.
        spill_path: returns a spilled sequence of paths from a provided
                   sequence of paths
        spill_paths: spills a path

    An analysis can overload either the specific sub-portions of surveyor
    (i.e, the tick_path and spill_path functions) or bigger and bigger pieces
    to implement more and more customizeable analyses.

    Surveyor provides at lest the following members:

        active - the paths that are still active in the analysis
        deadended - the paths that are still active in the analysis
        spilled - the paths that are still active in the analysis
        errored - the paths that have at least one error-state exit
    '''

    path_lists = [ 'active', 'deadended', 'spilled', 'suspended' ] # TODO: what about errored? It's a problem cause those paths are duplicates, and could cause confusion...

    def __init__(self, project, start=None, starts=None, max_active=None, max_concurrency=None, pickle_paths=None, save_deadends=None):
        '''
        Creates the Surveyor.

            @param project: the angr.Project to analyze
            @param starts: an exit to start the analysis on
            @param starts: the exits to start the analysis on. If neither start nor
                           starts are given, the analysis starts at p.initial_exit()
            @param max_active: the maximum number of paths to explore at a time
            @param max_concurrency: the maximum number of worker threads
            @param pickle_paths: pickle spilled paths to save memory
            @param save_deadends: save deadended paths
        '''

        self._project = project
        if project._parallel:
            self._max_concurrency = multiprocessing.cpu_count() if max_concurrency is None else max_concurrency
        else:
            self._max_concurrency = 1
        self._max_active = multiprocessing.cpu_count() if max_active is None else max_active
        self._pickle_paths = False if pickle_paths is None else pickle_paths
        self._save_deadends = True if save_deadends is None else save_deadends

        # the paths
        self.active = [ ]
        self.deadended = [ ]
        self.spilled = [ ]
        self.errored = [ ]
        self.suspended = [ ]

        self._current_step = 0

        if start is not None:
            self.analyze_exit(start)

        if starts is not None:
            for e in starts:
                self.analyze_exit(e)

        if start is None and starts is None:
            self.analyze_exit(project.initial_exit())

    #
    # Quick list access
    #

    @property
    def _a(self):
        return self.active[0]

    @property
    def _d(self):
        return self.deadended[0]

    @property
    def _spl(self):
        return self.spilled[0]

    @property
    def _e(self):
        return self.errored[0]

    @property
    def _su(self):
        return self.suspended[0]

    ###
    ### Overall analysis.
    ###

    def pre_tick(self):
        '''
        Provided for analyses to use for pre-tick actions.
        '''
        pass

    def post_tick(self):
        '''
        Provided for analyses to use for pre-tick actions.
        '''
        pass

    def step(self):
        '''
        Takes one step in the analysis (called by run()).
        '''

        self.pre_tick()
        self.tick()
        self.filter()
        self.spill()
        self.post_tick()
        self._current_step += 1

        l.debug("After iteration: %s", self)
        return self


    def run(self, n=None):
        '''
        Runs the analysis through completion (until done() returns True) or,
        if n is provided, n times.

            @params n: the maximum number of ticks
            @returns itself for chaining
        '''
        global STOP_RUNS, PAUSE_RUNS # pylint: disable=W0602,

        while not self.done and (n is None or n > 0):
            self.step()

            if STOP_RUNS:
                l.warning("%s stopping due to STOP_RUNS being set.", self)
                l.warning("... please call resume_analyses() and then this.run() if you want to resume execution.")
                break

            if PAUSE_RUNS:
                l.warning("%s pausing due to PAUSE_RUNS being set.", self)
                l.warning("... please call disable_singlestep() before continuing if you don't want to single-step.")

                try:
                    import ipdb as pdb # pylint: disable=F0401,
                except ImportError:
                    import pdb
                pdb.set_trace()

            if n is not None:
                n -= 1
        return self

    @property
    def done(self):
        '''
        True if the analysis is done.
        '''
        return len(self.active) == 0

    ###
    ### Utility functions.
    ###

    def active_exits(self, reachable=None, concrete=None, symbolic=None):
        '''
        Returns a sequence of reachable, flattened exits from all the currently
        active paths.
        '''
        all_exits = [ ]
        for p in self.active:
            all_exits += p.flat_exits(reachable=reachable, concrete=concrete, symbolic=symbolic)
        return all_exits

    def analyze_exit(self, e):
        '''
        Adds a path stemming from exit e to the analysis.
        '''
        self.active.append(Path(project=self._project, entry=e))

    def __repr__(self):
        return "%d active, %d spilled, %d deadended, %d errored" % (len(self.active), len(self.spilled), len(self.deadended), len(self.errored))

    ###
    ### Analysis progression
    ###

    def tick(self):
        '''
        Takes one step in the analysis. Typically, this moves all active paths
        forward.

            @returns itself, for chaining
        '''
        new_active = [ ]

        if self._max_concurrency > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers = self._max_concurrency) as executor:
                future_to_path = { executor.submit(self.tick_path, p): p for p in self.active }

                for future in concurrent.futures.as_completed(future_to_path):
                    p = future_to_path[future]
                    successors = future.result()

                    if len(p.errored) > 0:
                        l.debug("Path %s has yielded %d errored exits.", p, len(p.errored))
                        self.errored.append(self.suspend_path(p))
                    if len(successors) == 0:
                        l.debug("Path %s has deadended.", p)
                        if self._save_deadends:
                            self.deadended.append(self.suspend_path(p))
                        else:
                            self.deadended.append(p.backtrace)
                    else:
                        l.debug("Path %s has produced %d successors.", p, len(successors))

                    new_active.extend(successors)
        else:
            for p in self.active:
                successors = self.tick_path(p)

                if len(p.errored) > 0:
                    l.debug("Path %s has yielded %d errored exits.", p, len(p.errored))
                    self.errored.append(self.suspend_path(p))
                if len(successors) == 0:
                    l.debug("Path %s has deadended.", p)
                    if self._save_deadends:
                        self.deadended.append(self.suspend_path(p))
                    else:
                        self.deadended.append(p.backtrace)
                else:
                    l.debug("Path %s has produced %d successors.", p, len(successors))

                new_active.extend(successors)

        self.active = new_active
        return self

    def tick_path(self, p): # pylint: disable=R0201
        '''
        Ticks a single path forward. This should return a sequence of successor
        paths.
        '''
        #if hasattr(self, 'trace'):
        #   print "SETTING TRACE"
        #   __import__('sys').settrace(self.trace)
        return p.continue_path()

    ###
    ### Path termination.
    ###

    def filter_path(self, p): # pylint: disable=W0613,R0201
        '''
        Returns True if the given path should be kept in the analysis, False
        otherwise.
        '''
        return True

    def filter_paths(self, paths):
        '''
        Given a list of paths, returns filters them and returns the rest.
        '''
        return [ p for p in paths if self.filter_path(p) ]

    def filter(self):
        '''
        Filters the active paths, in-place.
        '''
        l.debug("before filter: %d paths", len(self.active))
        self.active = self.filter_paths(self.active)
        l.debug("after filter: %d paths", len(self.active))

    ###
    ### State explosion control (spilling paths).
    ###

    def path_comparator(self, a, b): # pylint: disable=W0613,R0201
        '''
        This function should compare paths a and b, to determine which should
        have a higher priority in the analysis. It's used as the cmp argument
        to sort.
        '''
        return 0

    def prioritize_paths(self, paths):
        '''
        This function is called to sort a list of paths, to prioritize
        the analysis of paths. Should return a list of paths, with higher-
        priority paths first.
        '''

        paths.sort(cmp=self.path_comparator)
        return paths

    def spill_paths(self, active, spilled): # pylint: disable=R0201
        '''
        Called with the currently active and spilled paths to spill some
        paths. Should return the new active and spilled paths.
        '''

        l.debug("spill_paths received %d active and %d spilled paths.", len(active), len(spilled))
        prioritized = self.prioritize_paths(active + spilled)
        new_active = prioritized[:self._max_active]
        new_spilled = prioritized[self._max_active:]
        l.debug("... %d active and %d spilled paths.", len(new_active), len(new_spilled))
        return new_active, new_spilled

    def spill(self):
        '''
        Spills/unspills paths, in-place.
        '''
        new_active, new_spilled = self.spill_paths(self.active, self.spilled)

        num_suspended = 0
        num_resumed = 0

        for p in new_active:
            if p in self.spilled:
                num_resumed += 1
                p.resume(self._project)

        for p in new_spilled:
            if p in self.active:
                num_suspended += 1
                self.suspend_path(p)

        l.debug("resumed %d and suspended %d", num_resumed, num_suspended)

        self.active, self.spilled = new_active, new_spilled

    def suspend_path(self, p):
        '''
        Suspends and returns a state.

        @param p: the path
        @returns the path
        '''
        p.suspend(do_pickle=self._pickle_paths)
        return p
