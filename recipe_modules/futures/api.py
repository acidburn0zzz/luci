# Copyright 2019 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

import gevent
import gevent.queue

import attr
from attr.validators import instance_of

from recipe_engine.recipe_api import RecipeApi, RequireClient


class _IWaitWrapper(object):
  __slots__ = ('_waiter', '_greenlets_to_futures')

  def __init__(self, futures, timeout, count):
    # pylint: disable=protected-access
    self._greenlets_to_futures = {fut._greenlet: fut for fut in futures}
    self._waiter = gevent.iwait(
        self._greenlets_to_futures.keys(), timeout, count)

  def __enter__(self):
    self._waiter.__enter__()
    return self

  def __exit__(self, typ, value, tback):
    return self._waiter.__exit__(typ, value, tback)

  def __iter__(self):
    return self

  def __next__(self):
    return self._greenlets_to_futures[self._waiter.__next__()]

  next = __next__


class FuturesApi(RecipeApi):
  """Provides access to the Recipe concurrency primitives."""
  concurrency_client = RequireClient('concurrency')

  class Timeout(Exception):
    """Raised from Future if the requested operation is not done in time."""

  @attr.s(frozen=True, slots=True)
  class Future(object):
    """Represents a unit of concurrent work.

    Modeled after Python 3's `concurrent.futures.Future`. We can expand this
    API carefully as we need it (e.g. potentially adding `cancel`).
    """

    _greenlet = attr.ib(
        validator=instance_of(gevent.Greenlet))  # type: gevent.Greenlet

    def result(self, timeout=None):
      """Blocks until this Future is done, then returns its value, or raises
      its exception.

      Args:
        * timeout (None|seconds) - How long to wait for the Future to be done.

      Returns the result if the Future is done.

      Raises the Future's exception, if the Future is done with an error.

      Raises Timeout if the Future is not done within the given timeout.
      """
      with gevent.Timeout(timeout, exception=FuturesApi.Timeout()):
        return self._greenlet.get()

    def done(self):
      """Returns True iff this Future is no longer running."""
      return self._greenlet.dead

    def exception(self, timeout=None):
      """Blocks until this Future is done, then returns (not raises) this
      Future's exception (or None if there was no exception).

      Args:
        * timeout (None|seconds) - How long to wait for the Future to be done.

      Returns the exception instance which would be raised from `result` if
      the Future is Done, otherwise None.

      Raises Timeout if the Future is not done within the given timeout.
      """
      with gevent.Timeout(timeout, exception=FuturesApi.Timeout()):
        done = gevent.wait([self._greenlet])[0]
        return done.exception


  def make_channel(self):
    """Returns a single-slot communication device for passing data and control
    between concurrent functions.

    This is useful for running 'background helper' type concurrent processes.

    NOTE: It is strongly discouraged to pass Channel objects outside of a recipe
    module. Access to the channel should be mediated via
    a class/contextmanager/function which you return to the caller, and the
    caller can call in a makes-sense-for-your-moudle's-API way.

    See ./tests/background_helper.py for an example of how to use a Channel
    correctly.

    It is VERY RARE to need to use a Channel. You should avoid using this unless
    you carefully consider and avoid the possibility of introducing deadlocks.

    Channels will raise ValueError if used with @@@annotation@@@ mode.
    """
    if not self.concurrency_client.supports_concurrency: # pragma: no cover
      # test mode always supports concurrency, hence the nocover
      raise ValueError('Channels are not allowed in @@@annotation@@@ mode')
    return gevent.queue.Channel()


  def spawn(self, func, *args, **kwargs):
    """Prepares a Future to run `func(*args, **kwargs)` concurrently.

    Any steps executed in `func` will only have manipulable StepPresentation
    within the scope of the executed function.

    Because this will spawn a greenlet on the same OS thread (and not,
    for example a different OS thread or process), `func` can easily be an
    inner function, closure, lambda, etc. In particular, func, args and kwargs
    do not need to be pickle-able.

    This function does NOT switch to the greenlet (you'll have to block on a
    future/step for that to happen). In particular, this means that the
    following pattern is safe:

        # self._my_future check + spawn + assignment is atomic because
        # no switch points occur.
        if not self._my_future:
          self._my_future = api.futures.spawn(func)

    NOTE: If used in @@@annotator@@@ mode, this will block on the completion of
    the Future before returning it.

    Kwargs:

      * __name (str) - If provided, will assign this name to the spawned
        greenlet. Useful if this greenlet ends up raising an exception, this
        name will appear in the stderr logging for the engine.
      * Everything else is passed to `func`.

    Returns a Future of `func`'s result.
    """
    ret = self.Future(self.concurrency_client.spawn(
        func, args, kwargs, kwargs.pop('__name', None)))
    if not self.concurrency_client.supports_concurrency: # pragma: no cover
      # test mode always supports concurrency, hence the nocover
      self.wait([ret])
    return ret

  def spawn_immediate(self, func, *args, **kwargs):
    """Returns a Future to the concurrently running `func(*args, **kwargs)`.

    This is like `spawn`, except that it IMMEDIATELY switches to the new
    Greenlet. You may want to use this if you want to e.g. launch a background
    step and then another step which waits for the daemon.

    Kwargs:

      * __name (str) - If provided, will assign this name to the spawned
        greenlet. Useful if this greenlet ends up raising an exception, this
        name will appear in the stderr logging for the engine.
      * Everything else is passed to `func`.

    Returns a Future of `func`'s result.
    """
    name = kwargs.pop('__name', None)
    chan = self.make_channel()
    def _immediate_runner():
      chan.get()
      return func(*args, **kwargs)
    ret = self.spawn(_immediate_runner, __name=name)
    chan.put(None)  # Pass execution to _immediate_runner
    return ret

  @staticmethod
  def wait(futures, timeout=None, count=None):
    """Blocks until `count` `futures` are done (or timeout occurs) then
    returns the list of done futures.

    This is analogous to `gevent.wait`.

    Args:
      * futures (List[Future]) - The Future objects to wait for.
      * timeout (None|seconds) - How long to wait for the Futures to be done.
        If we hit the timeout, wait will return even if we haven't reached
        `count` Futures yet.
      * count (None|int) - The number of Futures to wait to be done. If None,
        waits for all of them.

    Returns the list of done Futures, in the order in which they were done.
    """
    return list(_IWaitWrapper(futures, timeout, count))

  @staticmethod
  def iwait(futures, timeout=None, count=None):
    """Iteratively yield up to `count` Futures as they become done.


    This is analogous to `gevent.iwait`.

    Usage:

        for future in api.futures.iwait(futures):
          # consume future

    If you are not planning to consume the entire iwait iterator, you can
    avoid the resource leak by doing, for example:

        with api.futures.iwait(a, b, c) as iter:
          for future in iter:
            if future is a:
              break

    You might want to use `iwait` over `wait` if you want to process a group
    of Futures in the order in which they complete. Compare:

      for task in iwait(swarming_tasks):
        # task is done, do something with it

      vs

      while swarming_tasks:
        task = wait(swarming_tasks, count=1)[0]  # some task is done
        swarming_tasks.remove(task)
        # do something with it

    Args:
      * futures (List[Future]) - The Future objects to wait for.
      * timeout (None|seconds) - How long to wait for the Futures to be done.
      * count (None|int) - The number of Futures to yield. If None,
        yields all of them.

    Yields futures in the order in which they complete until we hit the
    timeout or count. May also be used with a context manager to avoid
    leaking resources if you don't plan on consuming the entire iterable.
    """
    return _IWaitWrapper(futures, timeout, count)