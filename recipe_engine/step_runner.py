# Copyright 2016 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

import cStringIO
import collections
import contextlib
import datetime
import json
import os
import re
import sys
import time
import tempfile
import traceback

from . import recipe_api
from . import recipe_test_api
from . import stream
from . import types
from . import util

from .third_party import subprocess42


if sys.platform == "win32":
  # Windows has a bad habit of opening a dialog when a console program
  # crashes, rather than just letting it crash.  Therefore, when a
  # program crashes on Windows, we don't find out until the build step
  # times out.  This code prevents the dialog from appearing, so that we
  # find out immediately and don't waste time waiting for a user to
  # close the dialog.
  import ctypes
  # SetErrorMode(
  #   SEM_FAILCRITICALERRORS|
  #   SEM_NOGPFAULTERRORBOX|
  #   SEM_NOOPENFILEERRORBOX
  # ).
  #
  # For more information, see:
  # https://msdn.microsoft.com/en-us/library/windows/desktop/ms680621.aspx
  ctypes.windll.kernel32.SetErrorMode(0x0001|0x0002|0x8000)


class _streamingLinebuf(object):
  def __init__(self):
    self.buffedlines = []
    self.extra = cStringIO.StringIO()

  def ingest(self, data):
    lines = data.splitlines()
    endedOnLinebreak = data.endswith("\n")

    if self.extra.tell():
      # we had leftovers from some previous ingest
      self.extra.write(lines[0])
      if len(lines) > 1 or endedOnLinebreak:
        lines[0] = self.extra.getvalue()
        self.extra = cStringIO.StringIO()
      else:
        return

    if not endedOnLinebreak:
      self.extra.write(lines[-1])
      lines = lines[:-1]

    self.buffedlines += lines

  def get_buffered(self):
    ret = self.buffedlines
    self.buffedlines = []
    return ret


class StepRunner(object):
  """A StepRunner is the interface to actually running steps.

  These can actually make subprocess calls (SubprocessStepRunner), or just
  pretend to run the steps with mock output (SimulationStepRunner).
  """
  @property
  def stream_engine(self):
    """Return the stream engine that this StepRunner uses, if meaningful.

    Users of this method must be prepared to handle None.
    """
    return None

  def open_step(self, step_dict):
    """Constructs an OpenStep object which can be used to actually run a step.

    step_dict parameters:
      name: name of the step, will appear in buildbots waterfall
      cmd: command to run, list of one or more strings
      cwd: absolute path to working directory for the command
      env: dict with overrides for environment variables
      allow_subannotations: if True, lets the step emit its own annotations
      trigger_specs: a list of trigger specifications, see also _trigger_builds.
      stdout: Path to a file to put step stdout into. If used, stdout won't
              appear in annotator's stdout (and |allow_subannotations| is
              ignored).
      stderr: Path to a file to put step stderr into. If used, stderr won't
              appear in annotator's stderr.
      stdin: Path to a file to read step stdin from.

    Returns an OpenStep object.
    """
    raise NotImplementedError()

  def run_recipe(self, universe, recipe, properties):
    """Run the recipe named |recipe|.

    Args:
      universe: The RecipeUniverse where the recipe lives.
      recipe: The recipe name (e.g. 'infra/luci_py')
      properties: a dictionary of properties to pass to the recipe

    Returns the recipe result.
    """
    raise NotImplementedError()

  @contextlib.contextmanager
  def run_context(self):
    """A context in which the entire engine run takes place.

    This is typically used to catch exceptions thrown by the recipe.
    """
    yield


class OpenStep(object):
  """An object that can be used to run a step.

  We use this object instead of just running directly because, after a step
  is run, it stays open (can be modified with step_text and links and things)
  until another step at its nest level or lower is started.
  """
  def run(self):
    """Starts the step, running its command."""
    raise NotImplementedError()

  def finalize(self):
    """Closes the step and finalizes any stored presentation."""
    raise NotImplementedError()

  @property
  def stream(self):
    """The stream.StepStream that this step is using for output.

    It is permitted to use this stream between run() and finalize() calls. """
    raise NotImplementedError()


class SubprocessStepRunner(StepRunner):
  """Responsible for actually running steps as subprocesses, filtering their
  output into a stream."""

  def __init__(self, stream_engine):
    self._stream_engine = stream_engine

  @property
  def stream_engine(self):
    return self._stream_engine

  def open_step(self, step_dict):
    allow_subannotations = step_dict.get('allow_subannotations', False)
    step_stream = self._stream_engine.new_step_stream(
        step_dict['name'],
        allow_subannotations=allow_subannotations)
    if not step_dict.get('cmd'):
      class EmptyOpenStep(OpenStep):
        def run(inner):
          if 'trigger_specs' in step_dict:
            self._trigger_builds(step_stream, step_dict['trigger_specs'])
          return types.StepData(step_dict, 0)

        def finalize(inner):
          step_stream.close()

        @property
        def stream(inner):
          return step_stream

      return EmptyOpenStep()

    step_dict, placeholders = render_step(
        step_dict, recipe_test_api.DisabledTestData())
    cmd = map(str, step_dict['cmd'])
    step_env = _merge_envs(os.environ, step_dict.get('env', {}))
    if 'nest_level' in step_dict:
      step_stream.step_nest_level(step_dict['nest_level'])
    self._print_step(step_stream, step_dict, step_env)

    class ReturnOpenStep(OpenStep):
      def run(inner):
        try:
          # Open file handles for IO redirection based on file names in
          # step_dict.
          handles = {
            'stdout': step_stream,
            'stderr': step_stream,
            'stdin': None,
          }
          for key in handles:
            fileName = step_dict.get(key)
            if fileName:
              handles[key] = open(fileName, 'rb' if key == 'stdin' else 'wb')
          # The subprocess will inherit and close these handles.
          retcode = self._run_cmd(
              cmd=cmd, timeout=step_dict.get('timeout'), handles=handles,
              env=step_env, cwd=step_dict.get('cwd'))
        except subprocess42.TimeoutExpired as e:
          # TODO(martiniss): mark as exception?
          raise recipe_api.StepTimeout(step_dict['name'], e.timeout)
        except OSError:
          with step_stream.new_log_stream('exception') as l:
            trace = traceback.format_exc().splitlines()
            for line in trace:
              l.write_line(line)
          step_stream.set_step_status('EXCEPTION')
          raise
        finally:
          # NOTE(luqui) See the accompanying note in stream.py.
          step_stream.reset_subannotation_state()

          if 'trigger_specs' in step_dict:
            self._trigger_builds(step_stream, step_dict['trigger_specs'])

        return construct_step_result(step_dict, retcode, placeholders)

      def finalize(inner):
        step_stream.close()

      @property
      def stream(inner):
        return step_stream

    return ReturnOpenStep()

  def run_recipe(self, universe_view, recipe, properties):
    with tempfile.NamedTemporaryFile() as f:
      cmd = [sys.executable,
             universe_view.universe.package_deps.engine_recipes_py,
             '--package=%s' % universe_view.universe.config_file.path, 'run',
             '--output-result-json=%s' % f.name, recipe]
      cmd.extend(['%s=%s' % (k,repr(v)) for k, v in properties.iteritems()])

      retcode = subprocess42.call(cmd)
      result = json.load(f)
      if retcode != 0:
        raise recipe_api.StepFailure(
          'depend on %s with properties %r failed with %d.\n'
          'Recipe result: %r' % (
              recipe, properties, retcode, result))
      return result

  @contextlib.contextmanager
  def run_context(self):
    """Swallow exceptions -- they will be captured and reported in the
    RecipeResult"""
    try:
      yield
    except Exception:
      pass

  def _render_step_value(self, value):
    if not callable(value):
      return value

    while hasattr(value, 'func'):
      value = value.func
    return getattr(value, '__name__', 'UNKNOWN_CALLABLE')+'(...)'

  def _print_step(self, step_stream, step, env):
    """Prints the step command and relevant metadata.

    Intended to be similar to the information that Buildbot prints at the
    beginning of each non-annotator step.
    """
    step_stream.write_line(' '.join(map(_shell_quote, step['cmd'])))
    step_stream.write_line('in dir %s:' % (step.get('cwd') or os.getcwd()))
    for key, value in sorted(step.items()):
      if value is not None:
        step_stream.write_line(
            ' %s: %s' % (key, self._render_step_value(value)))
    step_stream.write_line('full environment:')
    for key, value in sorted(env.items()):
      step_stream.write_line(' %s: %s' % (key, value))
    step_stream.write_line('')

  def _run_cmd(self, cmd, timeout, handles, env, cwd):
    """Runs cmd (subprocess-style).

    Args:
      cmd: a subprocess-style command list, with command first then args.
      handles: A dictionary from ('stdin', 'stdout', 'stderr'), each value
        being *either* a stream.StreamEngine.Stream or a python file object
        to direct that subprocess's filehandle to.
      env: the full environment to run the command in -- this is passed
        unaltered to subprocess.Popen.
      cwd: the working directory of the command.
    """
    fhandles = {}

    # If we are given StreamEngine.Streams, map them to PIPE for subprocess.
    # We will manually forward them to their corresponding stream.
    for key in ('stdout', 'stderr'):
      handle = handles.get(key)
      if isinstance(handle, stream.StreamEngine.Stream):
        fhandles[key] = subprocess42.PIPE
      else:
        fhandles[key] = handle

    # stdin must be a real handle, if it exists
    fhandles['stdin'] = handles.get('stdin')

    with _modify_lookup_path(env.get('PATH')):
      proc = subprocess42.Popen(
          cmd,
          env=env,
          cwd=cwd,
          detached=True,
          universal_newlines=True,
          **fhandles)

    # Safe to close file handles now that subprocess has inherited them.
    for handle in fhandles.itervalues():
      if isinstance(handle, file):
        handle.close()

    outstreams = {}
    linebufs = {}

    for key in ('stdout', 'stderr'):
      handle = handles.get(key)
      if isinstance(handle, stream.StreamEngine.Stream):
        outstreams[key] = handle
        linebufs[key] = _streamingLinebuf()

    if linebufs:
      # manually check the timeout, because we poll
      start_time = time.time()
      for pipe, data in proc.yield_any(timeout=1):
        if timeout and time.time() - start_time > timeout:
          # Don't know the name of the step, so raise this and it'll get caught
          raise subprocess42.TimeoutExpired(cmd, timeout)

        if pipe is None:
          continue
        buf = linebufs.get(pipe)
        if not buf:
          continue
        buf.ingest(data)
        for line in buf.get_buffered():
          outstreams[pipe].write_line(line)
    else:
      proc.wait(timeout)

    return proc.returncode

  def _trigger_builds(self, step, trigger_specs):
    assert trigger_specs is not None
    for trig in trigger_specs:
      builder_name = trig.get('builder_name')
      if not builder_name:
        raise ValueError('Trigger spec: builder_name is not set')

      changes = trig.get('buildbot_changes', [])
      assert isinstance(changes, list), 'buildbot_changes must be a list'
      changes = map(self._normalize_change, changes)

      step.trigger(json.dumps({
          'builderNames': [builder_name],
          'bucket': trig.get('bucket'),
          'changes': changes,
          # if True and triggering fails asynchronously, fail entire build.
          'critical': trig.get('critical', True),
          'properties': trig.get('properties'),
          'tags': trig.get('tags'),
      }, sort_keys=True))

  def _normalize_change(self, change):
    assert isinstance(change, dict), 'Change is not a dict'
    change = change.copy()

    # Convert when_timestamp to UNIX timestamp.
    when = change.get('when_timestamp')
    if isinstance(when, datetime.datetime):
      when = calendar.timegm(when.utctimetuple())
      change['when_timestamp'] = when

    return change


class SimulationStepRunner(StepRunner):
  """Pretends to run steps, instead recording what would have been run.

  This is the main workhorse of recipes.py simulation_test.  Returns the log of
  steps that would have been run in steps_ran.  Uses test_data to mock return
  values.
  """
  def __init__(self, stream_engine, test_data):
    self._test_data = test_data
    self._stream_engine = stream_engine
    self._step_history = collections.OrderedDict()

  @property
  def stream_engine(self):
    return self._stream_engine

  def open_step(self, step_dict):
    # We modify step_dict.  In particular, we add ~followup_annotations during
    # finalize, and depend on that side-effect being carried into what we
    # added to self._step_history, earlier.  So copy it here so at least we
    # keep the modifications local.
    step_dict = dict(step_dict)

    test_data_fn = step_dict.pop('step_test_data', recipe_test_api.StepTestData)
    step_test = self._test_data.pop_step_test_data(
        step_dict['name'], test_data_fn)
    step_dict, placeholders = render_step(step_dict, step_test)
    outstream = cStringIO.StringIO()

    # Layer the simulation step on top of the given stream engine.
    step_stream = stream.ProductStreamEngine.StepStream(
        self._stream_engine.new_step_stream(step_dict['name']),
        stream.BareAnnotationStepStream(outstream))

    class ReturnOpenStep(OpenStep):
      def run(inner):
        timeout = step_dict.get('timeout')
        if (timeout and step_test.times_out_after and
            step_test.times_out_after > timeout):
          raise recipe_api.StepTimeout(step_dict['name'], timeout)

        self._step_history[step_dict['name']] = step_dict
        return construct_step_result(step_dict, step_test.retcode, placeholders)

      def finalize(inner):
        # note that '~' sorts after 'z' so that this will be last on each
        # step. also use _step to get access to the mutable step
        # dictionary.
        lines = filter(None, outstream.getvalue().splitlines())
        if lines:
          # This magically floats into step_history, which we have already
          # added step_dict to.
          step_dict['~followup_annotations'] = lines

      @property
      def stream(inner):
        return step_stream

    return ReturnOpenStep()

  def run_recipe(self, universe, recipe, properties):
    return self._test_data.depend_on_data.pop(types.freeze((recipe, properties),))

  @contextlib.contextmanager
  def run_context(self):
    try:
      yield
    except Exception as ex:
      with self._test_data.should_raise_exception(ex) as should_raise:
        if should_raise:
          raise

    assert self._test_data.consumed, (
        "Unconsumed test data for steps: %s, (exception %s)" % (
            self._test_data.step_data.keys(),
            self._test_data.expected_exception))

  @property
  def steps_ran(self):
    return self._step_history.values()


# Result of 'render_step'.
Placeholders = collections.namedtuple(
    'Placeholders', ['inputs_cmd', 'outputs_cmd', 'stdout', 'stderr', 'stdin'])


# Singleton object to indicate a value is not set.
UNSET_VALUE = object()


def render_step(step, step_test):
  """Renders a step so that it can be fed to annotator.py.

  Args:
    step: The step to render.
    step_test: The test data json dictionary for this step, if any.
               Passed through unaltered to each placeholder.

  Returns the rendered step and a Placeholders object representing any
  placeholder instances that were found while rendering.
  """
  rendered_step = dict(step)

  # Process 'cmd', rendering placeholders there.
  input_phs = collections.defaultdict(lambda: collections.defaultdict(list))
  output_phs = collections.defaultdict(
      lambda: collections.defaultdict(collections.OrderedDict))
  new_cmd = []
  for item in step.get('cmd', []):
    if isinstance(item, util.Placeholder):
      module_name, placeholder_name = item.namespaces
      tdata = step_test.pop_placeholder(
          module_name, placeholder_name, item.name)
      new_cmd.extend(item.render(tdata))
      if isinstance(item, util.InputPlaceholder):
        input_phs[module_name][placeholder_name].append((item, tdata))
      else:
        assert isinstance(item, util.OutputPlaceholder), (
            'Not an OutputPlaceholder: %r' % item)
        # This assert ensures that:
        #   no two placeholders have the same name
        #   at most one placeholder has the default name
        assert item.name not in output_phs[module_name][placeholder_name], (
            'Step "%s" has multiple output placeholders of %s.%s. Please '
            'specify explicit and different names for them.' % (
              step['name'], module_name, placeholder_name))
        output_phs[module_name][placeholder_name][item.name] = (item, tdata)
    else:
      new_cmd.append(item)
  rendered_step['cmd'] = new_cmd

  # Process 'stdout', 'stderr' and 'stdin' placeholders, if given.
  stdio_placeholders = {}
  for key in ('stdout', 'stderr', 'stdin'):
    placeholder = step.get(key)
    tdata = None
    if placeholder:
      if key == 'stdin':
        assert isinstance(placeholder, util.InputPlaceholder), (
            '%s(%r) should be an InputPlaceholder.' % (key, placeholder))
      else:
        assert isinstance(placeholder, util.OutputPlaceholder), (
            '%s(%r) should be an OutputPlaceholder.' % (key, placeholder))
      tdata = getattr(step_test, key)
      placeholder.render(tdata)
      assert placeholder.backing_file is not None
      rendered_step[key] = placeholder.backing_file
    stdio_placeholders[key] = (placeholder, tdata)

  return rendered_step, Placeholders(
      inputs_cmd=input_phs, outputs_cmd=output_phs, **stdio_placeholders)


def construct_step_result(step, retcode, placeholders):
  """Constructs a StepData step result from step return data.

  The main purpose of this function is to add output placeholder results into
  the step result where output placeholders appeared in the input step.
  Also give input placeholders the chance to do the clean-up if needed.
  """

  step_result = types.StepData(step, retcode)

  class BlankObject(object):
    pass

  # Input placeholders inside step |cmd|.
  for _, pholders in placeholders.inputs_cmd.iteritems():
    for _, items in pholders.iteritems():
      for ph, td in items:
        ph.cleanup(td.enabled)

  # Output placeholders inside step |cmd|.
  for module_name, pholders in placeholders.outputs_cmd.iteritems():
    assert not hasattr(step_result, module_name)
    o = BlankObject()
    setattr(step_result, module_name, o)

    for placeholder_name, instances in pholders.iteritems():
      named_results = {}
      default_result = UNSET_VALUE
      for _, (ph, td) in instances.iteritems():
        result = ph.result(step_result.presentation, td)
        if ph.name is None:
          default_result = result
        else:
          named_results[ph.name] = result
      setattr(o, placeholder_name + "s", named_results)

      if default_result is UNSET_VALUE and len(named_results) == 1:
        # If only 1 output placeholder with an explicit name, we set the default
        # output.
        default_result = named_results.values()[0]

      # If 2+ placeholders have explicit names, we don't set the default output.
      if default_result is not UNSET_VALUE:
        setattr(o, placeholder_name, default_result)

  # Placeholders that are used with IO redirection.
  for key in ('stdout', 'stderr', 'stdin'):
    assert not hasattr(step_result, key)
    ph, td = getattr(placeholders, key)
    if ph:
      if isinstance(ph, util.OutputPlaceholder):
        setattr(step_result, key, ph.result(step_result.presentation, td))
      else:
        assert isinstance(ph, util.InputPlaceholder), (
            '%s(%r) should be an InputPlaceholder.' % (key, ph))
        ph.cleanup(td.enabled)

  return step_result


def _merge_envs(original, override):
  """Merges two environments.

  Returns a new environment dict with entries from |override| overwriting
  corresponding entries in |original|. Keys whose value is None will completely
  remove the environment variable. Values can contain %(KEY)s strings, which
  will be substituted with the values from the original (useful for amending, as
  opposed to overwriting, variables like PATH).
  """
  result = original.copy()
  if not override:
    return result
  for k, v in override.items():
    if v is None:
      if k in result:
        del result[k]
    else:
      result[str(k)] = str(v) % original
  return result


def _shell_quote(arg):
  """Shell-quotes a string with minimal noise.

  Such that it is still reproduced exactly in a bash/zsh shell.
  """

  arg = arg.encode('utf-8')

  if arg == '':
    return "''"
  # Normal shell-printable string without quotes
  if re.match(r'[-+,./0-9:@A-Z_a-z]+$', arg):
    return arg
  # Printable within regular single quotes.
  if re.match('[\040-\176]+$', arg):
    return "'%s'" % arg.replace("'", "'\\''")
  # Something complicated, printable within special escaping quotes.
  return "$'%s'" % arg.encode('string_escape')


@contextlib.contextmanager
def _modify_lookup_path(path):
  """Places the specified path into os.environ.

  Necessary because subprocess.Popen uses os.environ to perform lookup on the
  supplied command, and only uses the |env| kwarg for modifying the environment
  of the child process.
  """
  saved_path = os.environ['PATH']
  try:
    if path is not None:
      os.environ['PATH'] = path
    yield
  finally:
    os.environ['PATH'] = saved_path
