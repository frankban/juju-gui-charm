# This file is part of the Juju GUI, which lets users view and manage Juju
# environments within a graphical interface (https://launchpad.net/juju-gui).
# Copyright (C) 2013 Canonical Ltd.
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU Affero General Public License version 3, as published by
# the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranties of MERCHANTABILITY,
# SATISFACTORY QUALITY, or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Juju GUI server authentication management.

This module includes the pieces required to process user authentication:

    - User: a simple data structure representing a logged in or anonymous user;
    - authentication backends (GoBackend and PythonBackend): any object
      implementing the following interface:
        - get_request_id(data) -> id or None;
        - request_is_login(data) -> bool;
        - get_credentials(data) -> (str, str);
        - login_succeeded(data) -> bool.
      The only purpose of auth backends is to provide the logic to parse
      requests' data based on the API implementation currently in use. Backends
      don't know anything about the authentication process or the current user,
      and are not intended to store state: one backend (the one suitable for
      the current API implementation) is instantiated once when the application
      is bootstrapped and used as a singleton by all WebSocket requests;
    - AuthMiddleware: process authentication requests and responses, using
      the backend to parse the WebSocket messages, logging in the current user
      if the authentication succeeds.
"""

import datetime
import logging
import uuid

from tornado.ioloop import IOLoop


class User(object):
    """The current WebSocket user."""

    def __init__(self, username='', password='', is_authenticated=False):
        self.is_authenticated = is_authenticated
        self.username = username
        self.password = password

    def __repr__(self):
        if self.is_authenticated:
            status = 'authenticated'
        else:
            status = 'not authenticated'
        username = self.username or 'anonymous'
        return '<User: {} ({})>'.format(username, status)

    def __str__(self):
        return self.username.encode('utf-8')


class AuthMiddleware(object):
    """Handle user authentication.

    This class handles the process of authenticating the provided user using
    the given auth backend. Note that, since the GUI just disconnects when the
    user logs out, there is no need to handle the log out process.
    """

    def __init__(self, user, backend):
        self._user = user
        self._backend = backend
        self._request_id = None

    def in_progress(self):
        """Return True if the authentication is in progress, False otherwise.
        """
        return self._request_id is not None

    def process_request(self, data):
        """Parse the WebSocket data arriving from the client.

        Start the authentication process if data represents a login request
        performed by the GUI user.
        """
        backend = self._backend
        request_id = backend.get_request_id(data)
        if request_id is not None and backend.request_is_login(data):
            self._request_id = request_id
            credentials = backend.get_credentials(data)
            self._user.username, self._user.password = credentials

    def process_response(self, data):
        """Parse the WebSocket data arriving from the Juju API server.

        Complete the authentication process if data represents the response
        to a login request previously initiated. Authenticate the user if the
        authentication succeeded.
        """
        request_id = self._backend.get_request_id(data)
        if request_id == self._request_id:
            logged_in = self._backend.login_succeeded(data)
            if logged_in:
                logging.info('auth: user {} logged in'.format(self._user))
                self._user.is_authenticated = True
            else:
                self._user.username = self._user.password = ''
            self._request_id = None


class GoBackend(object):
    """Authentication backend for the Juju Go API implementation.

    A login request looks like the following:

        {
            'RequestId': 42,
            'Type': 'Admin',
            'Request': 'Login',
            'Params': {'AuthTag': 'user-admin', 'Password': 'ADMIN-SECRET'},
        }

    Here is an example of a successful login response:

        {'RequestId': 42, 'Response': {}}

    A login failure response is like the following:

        {
            'RequestId': 42,
            'Error': 'invalid entity name or password',
            'ErrorCode': 'unauthorized access',
            'Response': {},
        }
    """

    def get_request_id(self, data):
        """Return the request identifier associated with the provided data."""
        return data.get('RequestId')

    def request_is_login(self, data):
        """Return True if data represents a login request, False otherwise."""
        params = data.get('Params', {})
        return (
            data.get('Type') == 'Admin' and
            data.get('Request') == 'Login' and
            'AuthTag' in params and
            'Password' in params
        )

    def get_credentials(self, data):
        """Parse the provided login data and return username and password."""
        params = data['Params']
        return params['AuthTag'], params['Password']

    def login_succeeded(self, data):
        """Return True if data represents a successful login, False otherwise.
        """
        return 'Error' not in data


class PythonBackend(object):
    """Authentication backend for the Juju Python implementation.

    A login request looks like the following:

        {
            'request_id': 42,
            'op': 'login',
            'user': 'admin',
            'password': 'ADMIN-SECRET',
        }

    A successful login response includes these fields:

        {
            'request_id': 42,
            'op': 'login',
            'user': 'admin',
            'password': 'ADMIN-SECRET',
            'result': True,
        }

    A login failure response is like the following:

        {
            'request_id': 42,
            'op': 'login',
            'user': 'admin',
            'password': 'ADMIN-SECRET',
            'err': True,
        }
    """

    def get_request_id(self, data):
        """Return the request identifier associated with the provided data."""
        return data.get('request_id')

    def request_is_login(self, data):
        """Return True if data represents a login request, False otherwise."""
        op = data.get('op')
        return (op == 'login') and ('user' in data) and ('password' in data)

    def get_credentials(self, data):
        """Parse the provided login data and return username and password."""
        return data['user'], data['password']

    def login_succeeded(self, data):
        """Return True if data represents a successful login, False otherwise.
        """
        return data.get('result') and not data.get('err')


def get_backend(apiversion):
    """Return the auth backend instance to use for the given API version."""
    backend_class = {'go': GoBackend, 'python': PythonBackend}[apiversion]
    return backend_class()


class AuthenticationTokenHandler(object):
    """Handle requests related to authentication tokens.

    A token creation request looks like the following:

        {
            'RequestId': 42,
            'Type': 'GUIToken',
            'Request': 'Create',
            'Params': {},
        }

    Here is an example of a token creation response.

        {
            'RequestId': 42,
            'Response': {
                'Token': 'TOKEN-STRING',
                'Created': '2013-11-21T12:34:46.778866Z',
                'Expires': '2013-11-21T12:36:46.778866Z'
            }
        }

    A token authentication request looks like the following:

        {
            'RequestId': 42,
            'Type': 'GUIToken',
            'Request': 'Login',
            'Params': {'Token': 'TOKEN-STRING'},
        }

    Here is an example of a successful login response:

        {
            'RequestId': 42,
            'Response': {'AuthTag': 'user-admin', 'Password': 'ADMIN-SECRET'}
        }

    A login failure response is like the following:

        {
            'RequestId': 42,
            'Error': 'unknown, fulfilled, or expired token',
            'ErrorCode': 'unauthorized access',
            'Response': {},
        }

    Juju itself might return a failure response like the following, but this
    would be difficult or impossible to trigger as of this writing:

        {
            'RequestId': 42,
            'Error': 'invalid entity name or password',
            'ErrorCode': 'unauthorized access',
            'Response': {},
        }
    """

    def __init__(self, max_life=datetime.timedelta(minutes=2), io_loop=None):
        self._max_life = max_life
        if io_loop is None:
            io_loop = IOLoop.current()
        self._io_loop = io_loop
        self._data = {}

    def token_requested(self, data):
        """Does data represent a token creation request?  True or False."""
        return (
            'RequestId' in data and
            data.get('Type', None) == 'GUIToken' and
            data.get('Request', None) == 'Create'
        )

    def process_token_request(self, data, user, write_message):
        """Create a single-use, time-expired token and send it back."""
        token = uuid.uuid4().hex

        def expire_token():
            self._data.pop(token, None)
        handle = self._io_loop.add_timeout(self._max_life, expire_token)
        now = datetime.datetime.utcnow()
        # Stashing these is a security risk.  We currently deem this risk to
        # be acceptably small.  Even keeping an authenticated websocket in
        # memory seems to be of a similar risk profile, and we cannot operate
        # without that.
        self._data[token] = dict(
            username=user.username,
            password=user.password,
            handle=handle
            )
        write_message({
            'RequestId': data['RequestId'],
            'Response': {
                'Token': token,
                'Created': now.isoformat() + 'Z',
                'Expires': (now + self._max_life).isoformat() + 'Z'
            }
        })

    def authentication_requested(self, data):
        """Does data represent a token authentication request? True or False.
        """
        params = data.get('Params', {})
        return (
            'RequestId' in data and
            data.get('Type') == 'GUIToken' and
            data.get('Request') == 'Login' and
            'Token' in params
        )

    def process_authentication_request(self, data, write_message):
        """Get the credentials for the token, or send an error."""
        credentials = self._data.pop(data['Params']['Token'], None)
        if credentials is not None:
            self._io_loop.remove_timeout(credentials['handle'])
            return credentials['username'], credentials['password']
        else:
            write_message({
                'RequestId': data['RequestId'],
                'Error': 'unknown, fulfilled, or expired token',
                'ErrorCode': 'unauthorized access',
                'Response': {},
            })
            # None is an explicit return marker to say "I handled this".
            # It is returned by default.

    def process_authentication_response(self, data, user):
        """Make a successful token authentication response.

        This includes the username and password so that clients can then use
        them.  For instance, the GUI stashes them in session storage so that
        reloading the page does not require logging in again."""
        return {
            'RequestId': data['RequestId'],
            'Response': {'AuthTag': user.username, 'Password': user.password}
        }
