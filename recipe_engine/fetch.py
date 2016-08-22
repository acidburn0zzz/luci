# Copyright 2016 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

import base64
import functools
import json
import logging
import os
import random
import shutil
import sys
import tarfile
import tempfile
import time

# Add third party paths.
from . import env
import requests

import subprocess42
from google.protobuf import text_format

from . import package_pb2


class FetchError(Exception):
  pass


class UncleanFilesystemError(FetchError):
  pass


class FetchNotAllowedError(FetchError):
  pass


def _run_git(checkout_dir, *args):
  if sys.platform.startswith(('win', 'cygwin')):
    cmd = ['git.bat']
  else:
    cmd = ['git']

  if checkout_dir is not None:
    cmd += ['-C', checkout_dir]
  cmd += list(args)

  logging.info('Running: %s', cmd)
  return subprocess42.check_output(cmd)


def _retry(f):
  @functools.wraps(f)
  def wrapper(*args, **kwargs):
    delay = random.uniform(2, 5)
    for _ in range(5):
      try:
        return f(*args, **kwargs)
      except (requests.exceptions.RequestException,
              subprocess42.CalledProcessError):
        # Only retry specific errors that may be transient.
        logging.exception('retrying')
        time.sleep(delay)
        delay *= 2
    return f(*args, **kwargs)
  return wrapper


class Backend(object):
  @property
  def repo_type(self):
    """Returns repo type (see package_pb2.DepSpec)."""
    raise NotImplementedError()

  def branch_spec(self, branch):
    """Returns branch spec for given branch suitable for given git backend."""
    raise NotImplementedError()

  def checkout(self, repo, revision, checkout_dir, allow_fetch):
    """Checks out given |repo| at |revision| to |checkout_dir|.

    Network operations are performed only if |allow_fetch| is True.
    """
    raise NotImplementedError()

  def updates(self, repo, revision, checkout_dir, allow_fetch,
              other_revision, paths):
    """Returns a list of revisions between |revision| and |other_revision|.

    Network operations are performed only if |allow_fetch| is True.

    If |paths| is a non-empty list, the history is scoped just to these paths.
    """
    raise NotImplementedError()

  def commit_metadata(self, repo, revision, checkout_dir, allow_fetch):
    """Returns a dictionary of metadata about commit |revision|.

    The dictionary contains the following keys: author, message.
    """
    raise NotImplementedError()


class GitBackend(Backend):
  """GitBackend uses a local git checkout."""

  @property
  def repo_type(self):
    return package_pb2.DepSpec.GIT

  @staticmethod
  def branch_spec(branch):
    return 'origin/%s' % branch

  @_retry
  def checkout(self, repo, revision, checkout_dir, allow_fetch):
    logging.info('Freshening repository %s in %s', repo, checkout_dir)

    if not os.path.isdir(checkout_dir):
      if not allow_fetch:
        raise FetchNotAllowedError(
            'need to clone %s but fetch not allowed' % repo)
      _run_git(None, 'clone', '-q', repo, checkout_dir)
    elif not os.path.isdir(os.path.join(checkout_dir, '.git')):
      raise UncleanFilesystemError(
          '%s exists but is not a git repo' % checkout_dir)

    _run_git(checkout_dir, 'config', 'remote.origin.url', repo)
    try:
      _run_git(checkout_dir, 'rev-parse', '-q', '--verify',
               '%s^{commit}' % revision)
    except subprocess42.CalledProcessError:
      if not allow_fetch:
        raise FetchNotAllowedError(
            'need to fetch %s but fetch not allowed' % repo)
      _run_git(checkout_dir, 'fetch')
    _run_git(checkout_dir, 'reset', '-q', '--hard', revision)

  def updates(self, repo, revision, checkout_dir, allow_fetch,
              other_revision, paths):
    self.checkout(repo, revision, checkout_dir, allow_fetch)
    if allow_fetch:
      _run_git(checkout_dir, 'fetch')
    args = [
        'rev-list',
        '--reverse',
        '%s..%s' % (revision, other_revision),
    ]
    if paths:
      args.extend(['--'] + paths)
    return filter(bool, _run_git(checkout_dir, *args).strip().split('\n'))

  def commit_metadata(self, repo, revision, checkout_dir, allow_fetch):
    return {
      'author': _run_git(checkout_dir, 'show', '-s', '--pretty=%aE',
                         revision).strip(),
      'message': _run_git(checkout_dir, 'show', '-s', '--pretty=%B',
                          revision).strip(),
    }


class GitilesBackend(Backend):
  """GitilesBackend uses a repo served by Gitiles."""

  @property
  def repo_type(self):
    return package_pb2.DepSpec.GITILES

  @staticmethod
  def branch_spec(branch):
    return branch

  @_retry
  def checkout(self, repo, revision, checkout_dir, allow_fetch):
    logging.info('Freshening repository %s in %s', repo, checkout_dir)

    # TODO(phajdan.jr): implement caching.
    if not allow_fetch:
      raise FetchNotAllowedError(
          'need to download %s from gitiles but fetch not allowed' % repo)

    revision = self._resolve_revision(repo, revision)

    shutil.rmtree(checkout_dir, ignore_errors=True)

    recipes_cfg_url = '%s/+/%s/infra/config/recipes.cfg?format=TEXT' % (
        repo, requests.utils.quote(revision))
    logging.info('fetching %s' % recipes_cfg_url)
    recipes_cfg_request = requests.get(recipes_cfg_url)
    recipes_cfg_text = base64.b64decode(recipes_cfg_request.text)
    recipes_cfg_proto = package_pb2.Package()
    text_format.Merge(recipes_cfg_text, recipes_cfg_proto)
    recipes_path_rel = recipes_cfg_proto.recipes_path

    # Re-create recipes.cfg in |checkout_dir| so that the repo's recipes.py
    # can look it up.
    recipes_cfg_path = os.path.join(
        checkout_dir, 'infra', 'config', 'recipes.cfg')
    os.makedirs(os.path.dirname(recipes_cfg_path))
    with open(recipes_cfg_path, 'w') as f:
      f.write(recipes_cfg_text)

    recipes_path = os.path.join(checkout_dir, recipes_path_rel)
    if not os.path.exists(recipes_path):
      os.makedirs(recipes_path)

    archive_url = '%s/+archive/%s/%s.tar.gz' % (
        repo, requests.utils.quote(revision), recipes_path_rel)
    logging.info('fetching %s' % archive_url)
    archive_request = requests.get(archive_url)
    with tempfile.NamedTemporaryFile() as f:
      f.write(archive_request.content)
      f.flush()
      with tarfile.open(f.name) as archive_tarfile:
        archive_tarfile.extractall(recipes_path)

  def updates(self, repo, revision, checkout_dir, allow_fetch,
              other_revision, paths):
    if not allow_fetch:
      raise FetchNotAllowedError(
          'requested updates for %s from gitiles but fetch not allowed' % repo)

    revision = self._resolve_revision(repo, revision)
    other_revision = self._resolve_revision(repo, other_revision)
    # To include info about touched paths (tree_diff), pass name-status=1 below.
    log_json = self._fetch_gitiles_json(
        '%s/+log/%s..%s?name-status=1&format=JSON' % (
            repo, revision, other_revision))

    results = []
    for entry in log_json['log']:
      matched = False
      for path in paths:
        for diff_entry in entry['tree_diff']:
          if (diff_entry['old_path'].startswith(path) or
              diff_entry['new_path'].startswith(path)):
            matched = True
            break
        if matched:
          break
      if matched or not paths:
        results.append(entry['commit'])

    return list(reversed(results))

  def commit_metadata(self, repo, revision, checkout_dir, allow_fetch):
    if not allow_fetch:
      raise FetchNotAllowedError(
          ('requested commit metadata for %s (%s)from gitiles but fetch not '
           'allowed') % (repo, revision))
    rev_json = self._revision_metadata(repo, revision)
    return {
      'author': rev_json['author']['email'],
      'message': rev_json['message'],
    }

  def _revision_metadata(self, repo, revision):
    """Returns JSON metadata (in Gitiles format) for given revision."""
    return self._fetch_gitiles_json(
        '%s/+/%s?format=JSON' % (repo, requests.utils.quote(revision)))

  def _resolve_revision(self, repo, revision):
    """Returns a git sha corresponding to given revision.

    Examples of non-sha revision: origin/master, HEAD."""
    rev_json = self._revision_metadata(repo, revision)
    logging.info('resolved %s to %s', revision, rev_json['commit'])
    return rev_json['commit']

  def _fetch_gitiles_json(self, url):
    """Fetches JSON from Gitiles and returns parsed result."""
    logging.info('fetching %s', url)
    raw = requests.get(url).text
    if not raw.startswith(')]}\'\n'):
      raise FetchError('Unexpected gitiles response: %s' % raw)
    return json.loads(raw.split('\n', 1)[1])
