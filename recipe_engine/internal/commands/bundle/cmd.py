# Copyright 2019 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

"""Create a hermetically runnable recipe bundle (no git operations on startup).

Requires a git version >= 2.13+

This is done by packaging all the repos in RecipeDeps into a folder and then
generating an entrypoint script with `-O` override flags to this folder.

The general principle is that the input to bundle is:
  * The loaded RecipeDeps (derived from the main repo's recipes.cfg file). This
    is all the files on disk.
  * files tagged with the `recipes` gitattribute value (see
    `git help gitattributes`).
And the output is:
  * a runnable folder for the named repo

Some things that we'd want to do to make this better:
  * Allow this to fetch lazily from gitiles (no git clones)
    * will be necessary to support HUGE repos like chromium/src
  * Allow this to target a specific subset of runnable recipes (maybe)
    * prune down to ONLY the modules which are required to run those particular
      recipes.
    * this may be more trouble than it's worth

Included files

By default, bundle will include all recipes/ and recipe_modules/ files in your
repo, plus the `recipes.cfg` file, and excluding all json expectation files.

Recipe bundle also uses the standard `gitattributes` mechanism for tagging files
within the repo, and will also include these files when generating the bundle.
In particular, it looks for files tagged with the string `recipes`. As an
example, you could put this in a `.gitattributes` file in your repo:

```
*.py       recipes
*_test.py -recipes
```

That would include all .py files, but exclude all _test.py files. See the page
  `git help gitattributes`
For more information on how gitattributes work.
"""

from __future__ import absolute_import
import io
import logging
import ntpath
import os
import posixpath
import shutil
import stat
import subprocess
import sys

from collections import defaultdict

from PB import __path__ as PB_PATH # pylint: disable=import-error

from ... import simple_cfg
from ...recipe_deps import RecipeRepo, RecipeDeps

LOGGER = logging.getLogger(__name__)
GIT = 'git.bat' if sys.platform == 'win32' else 'git'


def _check(obj, typ):
  if not isinstance(obj, typ):
    msg = '%r was %s, expected %s' % (obj, type(obj).__name__, typ.__name__)
    LOGGER.debug(msg)
    raise TypeError(msg)


def _prepare_destination(destination):
  _check(destination, str)

  destination = os.path.abspath(destination)
  LOGGER.info('prepping destination %s', destination)
  if os.path.exists(destination):
    if os.listdir(destination):
      LOGGER.fatal(
        'directory %s exists and is non-empty! The directory must be empty or'
        ' missing to use it as a bundle target.', destination)
      sys.exit(1)
  else:
    os.makedirs(destination)
  return destination


def export_repo(repo, destination):
  """Copies all the recipe-relevant files for the repo to the given
  destination.

  Args:
    * repo (RecipeRepo) - The repo to export.
    * destination (str) - The absolute path we're exporting to (we'll export to
      a subfolder equal to `repo.name`).
  """
  _check(repo, RecipeRepo)
  _check(destination, str)

  bundle_dst = os.path.join(destination, repo.name)

  reldir = repo.simple_cfg.recipes_path
  if reldir:
    reldir += '/'

  args = [
    GIT, '-C', repo.path, 'ls-files', '--',
    ':(attr:recipes)',                       # anything tagged for recipes
    simple_cfg.RECIPES_CFG_LOCATION_REL,     # always grab recipes.cfg
    '%srecipes/**' % reldir,                 # all the recipes stuff
    '%srecipe_modules/**' % reldir,          # all the recipe_modules stuff
    '%srecipe_proto/**.proto' % reldir,      # all the protos in recipe_proto

    # And exclude all the json expectations
    ':(exclude)%s**/*.expected/*.json' % reldir,
  ]
  LOGGER.info('enumerating all recipe files: %r', args)
  to_copy = subprocess.check_output(args).splitlines()
  copy_map = defaultdict(set)
  for i in to_copy:
    if posixpath.sep != os.path.sep:
      i = i.replace(posixpath.sep, os.path.sep)
    while i:
      i, tail = os.path.split(i)
      base = os.path.join(repo.path, i) if i else repo.path
      copy_map[base].add(tail)

  def _ignore_fn(base, items):
    return set(items) - copy_map[base]

  shutil.copytree(repo.path, bundle_dst, ignore=_ignore_fn)


def export_protos(destination):
  """Exports the compiled protos for the bundle.

  The engine initialization process has already built all protos and made them
  importable as `PB`. We rely on `PB.__path__` because this allows the
  `--proto-override` flag to work.

  Args:
    * repo (RecipeRepo) - The repo to export.
    * destination (str) - The absolute path we're exporting to (we'll export to
      a subfolder `_pb/PB`).
  """
  shutil.copytree(
      PB_PATH[0], # root of generated PB folder.
      os.path.join(destination, '_pb', 'PB'),
      ignore=lambda _base, names: [n for n in names if n.endswith('.pyc')],
  )


TEMPLATE_SH = u"""#!/usr/bin/env bash
vpython -u ${BASH_SOURCE[0]%/*}/recipe_engine/recipe_engine/main.py \
"""

TEMPLATE_BAT = (
  u"""call vpython.bat -u "%~dp0\\recipe_engine\\recipe_engine\\main.py" ^
"""
)

def prep_recipes_py(recipe_deps, destination):
  """Prepares `recipes` and `recipes.bat` entrypoint scripts at the given
  destination.

  Args:
    * recipe_deps (RecipeDeps) - All loaded dependency repos.
    * destination (str) - The absolute path we're writing the scripts at.
  """
  _check(recipe_deps, RecipeDeps)
  _check(destination, str)

  overrides = recipe_deps.repos.keys()
  overrides.remove(recipe_deps.main_repo_id)

  LOGGER.info('prepping recipes.py for %s', recipe_deps.main_repo.name)
  recipes_script = os.path.join(destination, 'recipes')
  with io.open(recipes_script, 'w', newline='\n') as recipes_sh:
    recipes_sh.write(TEMPLATE_SH)

    pkg_path = posixpath.join(
        '${BASH_SOURCE[0]%%/*}/%s' % recipe_deps.main_repo.name,
        *simple_cfg.RECIPES_CFG_LOCATION_TOKS
    )
    recipes_sh.write(u' --package %s \\\n' % pkg_path)
    recipes_sh.write(u' --proto-override ${BASH_SOURCE[0]%/*}/_pb \\\n')
    for repo_name in overrides:
      recipes_sh.write(
          u' -O %s=${BASH_SOURCE[0]%%/*}/%s \\\n' % (repo_name, repo_name))
    recipes_sh.write(u' "$@"\n')
  os.chmod(recipes_script, os.stat(recipes_script).st_mode | stat.S_IXUSR)

  with io.open(recipes_script+'.bat', 'w', newline='\r\n') as recipes_bat:
    recipes_bat.write(TEMPLATE_BAT)

    pkg_path = ntpath.join(
      '"%%~dp0\\%s"' % recipe_deps.main_repo.name,
      *simple_cfg.RECIPES_CFG_LOCATION_TOKS
    )
    recipes_bat.write(u' --package %s ^\n' % pkg_path)
    recipes_bat.write(u' --proto-override "%~dp0\\_pb" ^\n')
    for repo_name in overrides:
      recipes_bat.write(u' -O %s=%%~dp0/%s ^\n' % (repo_name, repo_name))
    recipes_bat.write(u' %*\n')

def main(args):
  logging.basicConfig()
  destination = _prepare_destination(args.destination)
  for repo in args.recipe_deps.repos.values():
    export_repo(repo, destination)
  export_protos(destination)
  prep_recipes_py(args.recipe_deps, destination)
  LOGGER.info('done!')