# This file is part of the Juju GUI, which lets users view and manage Juju
# environments within a graphical interface (https://launchpad.net/juju-gui).
# Copyright (C) 2012-2013 Canonical Ltd.
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

"""
A composition system for creating backend objects.

Backends implement install(), start() and stop() methods. A backend is composed
of many mixins and each mixin will implement any/all of those methods and all
will be called. Backends additionally provide for collecting property values
from each mixin into a single final property on the backend. There is also a
feature for determining if configuration values have changed between old and
new configurations so we can selectively take action.
"""

import os
import shutil

import charmhelpers
import shelltoolbox
import utils


apt_get = shelltoolbox.command('apt-get')
SYS_INIT_DIR = '/etc/init/'


class GuiInstallMixin(object):
    """Provide for the GUI and its dependencies to be installed."""

    def install(self, backend):
        """Install the GUI and dependencies."""
        # If the given installable thing ("backend") requires one or more debs
        # that are not yet installed, install them.
        missing = utils.find_missing_packages(*backend.debs)
        if missing:
            utils.cmd_log(
                shelltoolbox.apt_get_install(*backend.debs))

        # If the source setting has changed since the last time this was run,
        # get the code, from either a static release or a branch as specified
        # by the souce setting, and install it.
        if backend.different('juju-gui-source'):
            # Get a tarball somehow and install it.
            release_tarball = utils.fetch_gui(
                backend.config['juju-gui-source'],
                backend.config['command-log-file'])
            utils.setup_gui(release_tarball)


class HaproxyApacheMixin(object):
    """Manage (install, start, stop, etc.) haproxy and Apache via Upstart."""

    upstart_scripts = ('haproxy.conf',)
    debs = ('curl', 'openssl', 'haproxy', 'apache2')

    def install(self, backend):
        """Set up haproxy and Apache startup configuration files."""
        utils.setup_apache()
        charmhelpers.log('Setting up haproxy and Apache startup scripts.')
        config = backend.config
        if backend.different(
                'ssl-cert-path', 'ssl-cert-contents', 'ssl-key-contents'):
            utils.save_or_create_certificates(
                config['ssl-cert-path'], config.get('ssl-cert-contents'),
                config.get('ssl-key-contents'))

        source_dir = os.path.join(os.path.dirname(__file__),  '..', 'config')
        for config_file in backend.upstart_scripts:
            shutil.copy(os.path.join(source_dir, config_file), SYS_INIT_DIR)

    def start(self, backend):
        with shelltoolbox.su('root'):
            charmhelpers.service_control(utils.APACHE, charmhelpers.RESTART)
            charmhelpers.service_control(utils.HAPROXY, charmhelpers.RESTART)

    def stop(self, backend):
        with shelltoolbox.su('root'):
            charmhelpers.service_control(utils.HAPROXY, charmhelpers.STOP)
            charmhelpers.service_control(utils.APACHE, charmhelpers.STOP)


class GuiStartMixin(object):
    """Start the GUI and expose it."""

    def start(self, backend):
        config = backend.config
        utils.start_gui(
            config['juju-gui-console-enabled'], config['login-help'],
            config['read-only'], config['staging'], config['ssl-cert-path'],
            config['charmworld-url'], config['serve-tests'],
            secure=config['secure'], sandbox=config['sandbox'],
            use_analytics=config['use-analytics'],
            default_viewmode=config['default-viewmode'],
            show_get_juju_button=config['show-get-juju-button'])

        charmhelpers.open_port(80)
        charmhelpers.open_port(443)


class SandboxMixin(object):
    pass


class PythonMixin(object):
    """Manage the real PyJuju backend."""

    def install(self, backend):
        config = backend.config
        if (not os.path.exists(utils.JUJU_DIR) or
                backend.different('staging', 'juju-api-branch')):
            utils.fetch_api(config['juju-api-branch'])

    def start(self, backend):
        utils.start_agent(backend.config['ssl-cert-path'])

    def stop(self, backend):
        charmhelpers.service_control(utils.AGENT, charmhelpers.STOP)


class ImprovMixin(object):
    """Manage the improv backend when on staging."""

    debs = ('zookeeper',)

    def install(self, backend):
        config = backend.config
        if (not os.path.exists(utils.JUJU_DIR) or
                backend.different('staging', 'juju-api-branch')):
            utils.fetch_api(config['juju-api-branch'])

    def start(self, backend):
        config = backend.config
        utils.start_improv(
            config['staging-environment'], config['ssl-cert-path'])

    def stop(self, backend):
        charmhelpers.service_control(utils.IMPROV, charmhelpers.STOP)


class GoMixin(object):
    """Manage the real Go juju-core backend."""

    debs = ('python-yaml',)

    def install(self, backend):
        # When juju-core deploys the charm, the charm directory (which hosts
        # the GUI itself) is permissioned too strictly; set the perms on that
        # directory to be friendly for Apache.
        # Bug: 1202772
        utils.cmd_log(shelltoolbox.run('chmod', '+x', utils.CURRENT_DIR))


def chain_methods(name):
    """Helper to compose a set of mixin objects into a callable.

    Each method is called in the context of its mixin instance, and its
    argument is the Backend instance.
    """
    # Chain method calls through all implementing mixins.
    def method(self):
        for mixin in self.mixins:
            a_callable = getattr(type(mixin), name, None)
            if a_callable:
                a_callable(mixin, self)

    method.__name__ = name
    return method


def merge_properties(name):
    """Helper to merge a property from a set of mixin objects into a unified
    set.
    """
    @property
    def method(self):
        result = set()
        for mixin in self.mixins:
            segment = getattr(type(mixin), name, None)
            if segment and isinstance(segment, (list, tuple, set)):
                result |= set(segment)

        return result
    return method


class Backend(object):
    """Compose methods and policy needed to interact with a Juju backend."""

    def __init__(self, config=None, prev_config=None):
        """Generate a list of mixin classes that implement the backend, working
        through composition.

        'config' is a dict which typically comes from the JSON de-serialization
            of config.json in JujuGUI.
        'prev_config' is a dict used to compute the differences. If it is not
            passed, all current config values are considered new.
        """
        if config is None:
            config = utils.get_config()
        self.config = config
        if prev_config is None:
            prev_config = {}
        self.prev_config = prev_config

        # We always install the GUI.
        mixins = [GuiInstallMixin]

        sandbox = config.get('sandbox', False)
        staging = config.get('staging', False)

        if utils.legacy_juju():
            if staging:
                mixins.append(ImprovMixin)
            elif sandbox:
                mixins.append(SandboxMixin)
            else:
                mixins.append(PythonMixin)
        else:
            if staging:
                raise ValueError('Unable to use staging with go backend')
            elif sandbox:
                raise ValueError('Unable to use sandbox with go backend')
            mixins.append(GoMixin)

        # All backends need to install, start, and stop the services that
        # provide the GUI.
        mixins.append(GuiStartMixin)
        mixins.append(HaproxyApacheMixin)

        # Record our choice mapping classes to instances.
        for i, b in enumerate(mixins):
            if callable(b):
                mixins[i] = b()
        self.mixins = mixins

    def different(self, *keys):
        """Return a boolean indicating if the current config
        value differs from the config value passed in prev_config
        with respect to any of the passed in string keys.
        """
        # Minimize lookups inside the loop, just because.
        current, previous = self.config.get, self.prev_config.get
        return any(current(key) != previous(key) for key in keys)

    ## Composed Methods
    install = chain_methods('install')
    start = chain_methods('start')
    stop = chain_methods('stop')

    ## Merged Properties
    dependencies = merge_properties('dependencies')
    build_dependencies = merge_properties('build_dependencies')
    staging_dependencies = merge_properties('staging_dependencies')

    repositories = merge_properties('repositories')
    debs = merge_properties('debs')
    upstart_scripts = merge_properties('upstart_scripts')
