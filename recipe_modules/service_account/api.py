# Copyright 2017 The LUCI Authors. All rights reserved.
# Use of this source code is governed under the Apache License, Version 2.0
# that can be found in the LICENSE file.

"""API for getting OAuth2 access tokens for LUCI tasks or private keys.

This is a thin wrapper over the luci-auth go executable (
https://godoc.org/go.chromium.org/luci/auth/client/cmd/luci-auth).

Depends on luci-auth to be in PATH.
"""

from recipe_engine import recipe_api


class ServiceAccountApi(recipe_api.RecipeApi):

  class ServiceAccount(object):
    """Represents some service account available to the recipe.

    Grab an instance of this class via 'default()' or 'from_credentials_json()'.
    """

    def __init__(self, api, title, key_path):
      self._api = api
      self._title = title
      self._key_path = key_path   # or None to use default LUCI account

    def get_access_token(self, scopes=None):
      """Returns an access token for this service account.

      Token's lifetime is guaranteed to be at least 3 minutes and at most 45.

      Args:
        scopes: list of OAuth scopes for new token, default is [userinfo.email].
      """
      extra_args = []
      if self._key_path:
        extra_args = ['-service-account-json', self._key_path]
      return self._api._get_token(self._title, extra_args, scopes)

    def get_email(self):
      """Returns the service account email."""
      # TODO(vadimsh): This is implementable, but no one needs it now. Ping
      # vadimsh if needed.
      raise NotImplementedError()  # pragma: no cover


  def default(self):
    """Returns an account associated with the task.

    On LUCI, this is default account exposed through LUCI_CONTEXT["local_auth"]
    protocol. When running locally this is an account the user logged in via
    "luci-auth login ..." command prior to running the recipe.
    """
    return self.ServiceAccount(self, 'default account', None)

  def from_credentials_json(self, key_path):
    """Returns a service account based on a JSON credentials file.

    This is the file generated by Cloud Console when creating a service account
    key. It contains the private key inside.

    Args:
      key_path: (str|Path) object pointing to a service account JSON key.
    """
    return self.ServiceAccount(self, self.m.path.split(key_path)[1], key_path)


  def _get_token(self, title, extra_args, scopes):
    cmd = ['luci-auth', 'token'] + extra_args
    if scopes:
      cmd += ['-scopes', ' '.join(sorted(scopes))]
    # Due to Swarming, 5 min is the hard upper limit.
    cmd += ['-lifetime', '3m']
    step_result = self.m.step(
        'get access token for %s' % title,
        cmd,
        infra_step=True,
        stdout=self.m.raw_io.output_text(),
        step_test_data=lambda: self.m.raw_io.test_api.stream_output(
            'extra.secret.token.should.not.be.logged', stream='stdout'))
    return step_result.stdout.strip()