# -*- coding: utf-8 -*-
# http://www.apache.org/licenses/LICENSE-2.0.txt
#
# Copyright 2016 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import json
import logging
import platform
import sys
import time
from abc import ABCMeta, abstractmethod
from concurrent import futures
from enum import Enum
from http.server import BaseHTTPRequestHandler, HTTPServer
from past.builtins import basestring
from socket import error as socket_error
from timeit import default_timer as timer
from threading import Thread

import grpc
import six

from .plugin_pb2 import GetConfigPolicyReply
from .config_map import ConfigMap

LOG = logging.getLogger(__name__)


class _Timer(object):
    """Timer for diagnostic timing"""
    def __enter__(self):
        self._start = timer()

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._end = timer()

    def elapsed(self):
        elapsed = self._end - self._start
        # start with μs
        elapsed = elapsed * 1000000
        unit = "μs"
        if elapsed > 1000:
            # switch to ms
            elapsed = elapsed / 1000
            unit = "ms"
        return "{:.3f} {}".format(float(elapsed), unit)


def _make_standalone_handler(preamble):
    """Class factory used so that preamble can be passed to :py:class:`_StandaloneHandler`
     without use of static members"""
    class _StandaloneHandler(BaseHTTPRequestHandler, object):
        """HTTP Handler for standalone mode"""

        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'application/json; charset=utf-8')
            self.send_header('Content-length', len(preamble))
            self.end_headers()
            self.wfile.write(preamble.encode('utf-8'))

        def log_message(self, format, *args):
            # suppress logging on requests
            return
    return _StandaloneHandler


class _Flags(object):
    """Command line flags container"""
    def __init__(self):
        self._flags = {}

    def add(self, name, flag_type, description, default=None):
        """Add single command line flag

        Arguments:
            name (:obj:`str`): Name of flag used in command line
            flag_type (:py:class:`snap_plugin.v1.plugin.FlagType`):
                Indication if flag should store value or is simple bool flag
            description (:obj:`str`): Flag description used in command line
            default (:obj:`object`, optional): Optional default value for flag

        Raises:
            TypeError: Provided wrong arguments or arguments of wrong types, method will raise TypeError

        """
        if not(isinstance(name, basestring) and isinstance(description, basestring)):
            raise TypeError("Name and description should be strings, are of type {} and {}"
                            .format(type(name), type(description)))
        if not(isinstance(flag_type, FlagType)):
            raise TypeError("Flag type should be of type FlagType, is of {}".format(type(flag_type)))

        if name not in self._flags:
            if default is not None:
                if default is not False:
                    description += " (default: %(default)s)"
                self._flags[name] = (flag_type, description, default)
            else:
                self._flags[name] = (flag_type, description)

    def add_multiple(self, flags):
        """Add multiple command line flags

        Arguments:
            flags (:obj:`list` of :obj:`tuple`): List of flags
                in tuples (name, flag_type, description, (optional) default)

        Raises:
            TypeError: Provided wrong arguments or arguments of wrong types, method will raise TypeError
        """
        if not isinstance(flags, list):
            raise TypeError("Expected list of flags, got object of type{}".format(type(flags)))
        for flag in flags:
            if not isinstance(flag, tuple) or not(len(flag) == 3 or len(flag) == 4):
                raise TypeError("Expected tuple of length 3 or 4, got {}".format(flag))
            if len(flag) == 3:
                self.add(*flag)
            else:
                self.add(*flag[:3], default=flag[3])

    def __iter__(self):
        for name, atr in self._flags.items():
            yield (name,) + atr


class _EnumEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Enum):
            return obj.value
        return json.JSONEncoder.default(self, obj)


class RoutingStrategy(Enum):
    """Plugin routing strategies

    - lru (default): Least recently used
        Calls are routed to the least recently called plugin instance.
    - sticky: Sticky based routing
        Calls are routed to the same instance of the running plugin.  See
        :py:class:`Meta` its attribute `concurrency_count`.
    - config: Config based routing
        Calls are routed based on the configuration matching.  In this case
        multiple tasks could be configured with the same configuration in which
        case the framework will route calls to only those running instances with
        a configuration matching the task that is firing.

    """
    lru = 0
    sticky = 1
    config = 2


class PluginType(Enum):
    """Plugin types """
    collector = 0
    processor = 1
    publisher = 2
    stream_collector = 3


class PluginResponseState(Enum):
    """Plugin response states"""
    plugin_success = 0
    plugin_failure = 1


class RPCType(Enum):
    """Snap RPC types"""
    native = 0
    json = 1
    grpc = 2
    grpc_stream = 3

    def __str__(self):
        switch = {
            0: "Native",
            1: "JSON",
            2: "gRPC",
            3: "gRPCStream"
        }
        return switch[self.value]


class PluginMode(Enum):
    """Plugin operating modes"""
    normal = 0
    diagnostics = 1
    standalone = 2


class FlagType(Enum):
    """Possible flag types"""
    value = 0
    toggle = 1


class Meta(object):
    """Snap plugin meta

    Arguments:
        type (:py:class:`PluginType`): Plugin type
        name (:obj:`string`): Plugin name
        version (:obj:`int`): Plugin version
        concurrency_count (:obj:`int`): ConcurrencyCount is the max number
            concurrent calls the plugin may take. If there are 5 tasks using the
            plugin and concurrency count is 2 there will be 3 plugins running.
        routing_strategy (:py:class:`RoutingStrategy`): RoutingStrategy will
            override the routing strategy this plugin requires.  The default
            routing strategy is least-recently-used.
        exclusive (:obj:`bool`): Exclusive results in a single instance of the
            plugin running regardless the number of tasks using the plugin.
        cache_ttl (:obj:`int`): CacheTTL will override the default cache TTL
            for the provided plugin and the metrics it exposes (default=500ms).
        rpc_type (:py:class:`RPCType`)> RPC type
        rpc_version (:obj:`int`): RPC version
        unsecure (:obj:`bool`): Unsecure
    """
    def __init__(self,
                 type,
                 name,
                 version,
                 concurrency_count=5,
                 routing_strategy=RoutingStrategy.lru,
                 exclusive=False,
                 cache_ttl=None,
                 rpc_type=RPCType.grpc,
                 rpc_version=1,
                 unsecure=True):
        self.name = name
        self.version = version
        setattr(sys.modules["snap_plugin.v1"], "PLUGIN_VERSION", version)
        self.type = type
        self.concurrency_count = concurrency_count
        self.routing_strategy = routing_strategy
        self.exclusive = exclusive
        self.cache_ttl = cache_ttl
        self.rpc_type = rpc_type
        self.rpc_version = rpc_version
        self.unsecure = unsecure


@six.add_metaclass(ABCMeta)
class Plugin(object):
    """Abstract base class for Collector, Processor and Publisher plugins.

    Plugin authors should not inherit directly from this class rather they
    should inherit from :py:class:`snap_plugin.v1.collector.Collector`,
    :py:class:`snap_plugin.v1.processor.Processor` or
    :py:class:`snap_plugin.v1.publisher.Publisher`.
    """

    def __init__(self):
        self.meta = None
        self.proxy = None
        self.server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
        self._port = 0
        self._last_ping = time.time()
        self._shutting_down = False
        self._monitor = None
        self._mode = PluginMode.normal
        self._config = {}
        self._flags = _Flags()
        self.standalone_server = None

        # init argparse module and add arguments
        self._parser = argparse.ArgumentParser(description="%(prog)s - a Snap framework plugin.",
                                               usage="%(prog)s [options]",
                                               formatter_class=lambda prog:
                                               argparse.HelpFormatter(prog, max_help_position=30))
        self._parser.add_argument("framework_config", nargs="?", default=None, help=argparse.SUPPRESS)

        flags = [
            ("config", FlagType.value, "JSON Snap global config"),
            ("stand-alone", FlagType.toggle, "enable stand alone mode"),
            ("stand-alone-port", FlagType.value, "http port for stand alone mode", 8181),
            ("log-level", FlagType.value, "logging level 0:panic - 5:debug", 3)
        ]
        self._flags.add_multiple(flags)

    def ping(self):
        """Ping responds to clients providing proof of life

        The Snap framework will ping plugins every 1.5s to confirm they are not
        hung.
        """
        self._last_ping = time.time()

    def stop_plugin(self):
        """Stops the plugin"""
        LOG.debug("plugin stopping")
        self._shutting_down = True
        _stop_event = self.server.stop(0)
        while not _stop_event.is_set():
            time.sleep(.1)
        LOG.debug("plugin stopped")

    def start_plugin(self):
        """Starts the Plugin
        
        Starting a plugin in normal mode includes:
            - Finding an available port
            - Starting the gRPC server
            - Printing to STDOUT data (JSON) for handshaking with Snap

        Collector plugin started without config provided in command line arguments
        will run in diagnostic mode, which will print out following info:
            - Runtime details
            - Config policy
            - Metric catalog
            - Current values of metrics
        """
        # CLI argument parsing can be started only after start_plugin call
        # to let plugin authors add their own flags
        self._parse_args()

        LOG.debug("plugin start called..")
        if self._mode == PluginMode.normal:
            # start grpc server
            preamble = self._generate_preamble_and_serve()
            sys.stdout.write(preamble)
            sys.stdout.flush()
            self._monitor.start()
            self._monitor.join()
            sys.exit()
        elif self._mode == PluginMode.standalone:
            preamble = self._generate_preamble_and_serve()
            try:
                handler = _make_standalone_handler(preamble)
                self.standalone_server = HTTPServer(('', int(self._args.stand_alone_port)), handler)
            except (OSError, socket_error) as err:
                if err.errno == 98:
                    LOG.error("Port {} already in use.".format(self._args.stand_alone_port))
                elif err.errno == 13:
                    LOG.error("Port numbers below 1000 can be used only by privileged users.")
                else:
                    # unexpected error, re-raise
                    raise
            else:
                try:
                    sys.stdout.write("Plugin loaded at {}:{}\n".format(*self.standalone_server.server_address))
                    sys.stdout.flush()
                    self.standalone_server.serve_forever()
                except KeyboardInterrupt:
                    self.standalone_server.socket.close()

        elif self._mode == PluginMode.diagnostics and self.meta.type == PluginType.collector:
            self._print_diagnostic()
        else:
            sys.stdout.write("At the time being, plugin diagnostic is supported only by Collector plugins.")
            sys.stdout.flush()

    def _generate_preamble_and_serve(self):
        self._port = self.server.add_insecure_port('127.0.0.1:{!s}'.format(0))
        self.server.start()
        return json.dumps(
            {
                "Meta": {
                    "Name": self.meta.name,
                    "Version": self.meta.version,
                    "Type": self.meta.type,
                    "RPCType": self.meta.rpc_type,
                    "RPCVersion": self.meta.rpc_version,
                    "ConcurrencyCount": self.meta.concurrency_count,
                    "Exclusive": self.meta.exclusive,
                    "Unsecure": self.meta.unsecure,
                    "CacheTTL": self.meta.cache_ttl,
                    "RoutingStrategy": self.meta.routing_strategy,
                },
                "ListenAddress": "127.0.0.1:{!s}".format(self._port),
                "Token": None,
                "PublicKey": None,
                "Type": self.meta.type,
                "ErrorMessage": None,
                "State": PluginResponseState.plugin_success,
            },
            cls=_EnumEncoder
        ) + "\n"

    def _parse_args(self):
        """Parse command line arguments, parse config and initialize monitor"""

        # set name and add version argument from Meta
        self._parser.prog = self.meta.name
        self._parser.add_argument("-v", "--version", action="version", version="%(prog)s v{}".format(self.meta.version),
                                  help="show plugin's version number and exit")

        # add every flag from Flags object to parser
        for flag in list(sorted(self._flags, key=lambda flag: flag[0])):
            kwargs = {"dest": flag[0], "help": flag[2], "action": "store_true"}
            if '-' in flag[0]:
                kwargs["dest"] = flag[0].replace('-', '_')
            if flag[1] == FlagType.value:
                kwargs["action"] = "store"
                kwargs["metavar"] = "value"
                if len(flag) == 4:
                    kwargs["default"] = flag[3]

            self._parser.add_argument("--{}".format(flag[0]), **kwargs)

        self._args = self._parser.parse_args()
        self._config["LogLevel"] = int(self._args.log_level)

        if self._args.framework_config is not None:
            # if there is a config provided by the framework, we operate in normal mode
            self._args.config = self._args.framework_config
        elif self._args.stand_alone:
            # if user provided stand-alone flag, we run in standalone mode
            self._mode = PluginMode.standalone
        else:
            # if config wasn't provided by the framework and user didn't use standalone flag, we run diagnostics
            self._mode = PluginMode.diagnostics

        # process the config (valid json) provided by the framework or the user
        if self._args.config is not None:
            try:
                self._config = json.loads(self._args.config)
            except ValueError:
                LOG.warning("Invalid config provided: expected JSON (provided={}).".format(self._args.config))
                self._config = {}

        self._set_log_level()
        self._monitor = Thread(
            target=_monitor,
            args=(self.last_ping,
                  self.stop_plugin,
                  self._is_shutting_down),
            kwargs={"timeout": self._get_ping_timeout_duration()})

    def _set_log_level(self):
        """Sets the log level provided by the framework.

        If no log level is passed the level is set to Warn.

        Snap communicates the following log levels to plugins.
            - 1 debug
            - 2 info
            - 3 warn
            - 4 error
            - 5 fatal
            - 6 panic
        """
        if "LogLevel" in self._config:
            if 5 >= self._config["LogLevel"] >= 1:
                # Multiplying the provided level by 10 will convert them to what
                # the python logging module uses.
                LOG.setLevel(self._config["LogLevel"] * 10)
                LOG.info("Setting loglevel to %s.", LOG.getEffectiveLevel())
            else:
                LOG.error("The log level should be between 1 and 5." +
                          " (given={})", self._config["LogLevel"])
        else:
            # set to warn if log level not provided
            LOG.setLevel(30)

    def _get_ping_timeout_duration(self):
        """Gets the ping timeout duration provided by the framework.

        The ping timeout duration returned from the framework is in ms. so we
        convert it to seconds.  If the is no timeout provided by the framework
        we return the default of 5(seconds).
        """
        if "PingTimeoutDuration" in self._config:
            return self._config["PingTimeoutDuration"] / 1000
        else:
            return 5

    def last_ping(self):
        """Returns the epoch time when the last ping was received"""
        return self._last_ping

    def _is_shutting_down(self):
        """Returns bool indicating whether the plugin is shutting down"""
        return self._shutting_down

    def _print_diagnostic(self):
        """Prints diagnostic information"""
        diagnostics_timer, print_timer = _Timer(), _Timer()

        with diagnostics_timer:
            # runtime details
            with print_timer:
                sys.stdout.write("Runtime Details:\n\tPlugin Name: {}, Plugin Version: {}\n"
                                 .format(self.meta.name, self.meta.version))
                sys.stdout.write("\tRPC Type: {}, RPC Version: {}\n"
                                 .format(self.meta.rpc_type, self.meta.rpc_version))
                sys.stdout.write("\tPlatform: {}\n\tArchitecture: {}\n\tPython Version: {}\n"
                                 .format(platform.platform(), platform.machine(), platform.python_version()))

            sys.stdout.write("Printing runtime details took {}\n\n".format(print_timer.elapsed()))
            sys.stdout.flush()

            # config policy
            with print_timer:
                sys.stdout.write("Config Policy:\n")
                policy_table = []
                config_missing = []
                defaults = []
                cpolicy = self.get_config_policy()
                for kt, policy in cpolicy.policies:
                    entries, missing, t_defaults = self._parse_policy_namespaces(policy, kt)
                    policy_table.extend(entries)
                    config_missing.extend(missing)
                    defaults.extend(t_defaults)

                headers = ["NAMESPACE", "KEY", "TYPE", "REQUIRED", "DEFAULT", "MINIMUM", "MAXIMUM"]
                sys.stdout.write(_tabulate(policy_table, headers))

                # return if there are any missing required config entries
                for missing in config_missing:
                    LOG.error("{} required by plugin and not provided in config".format(missing))
                if len(config_missing) != 0:
                    LOG.error('You can provide config in form of "--config \'{"key": "value", "answer": 42}\'"')
                    return

            sys.stdout.write("Printing config policy took {}\n\n".format(print_timer.elapsed()))
            sys.stdout.flush()

            # metric catalog
            with print_timer:
                sys.stdout.write("Metric catalog will be updated to include following namespaces:\n")

                metrics = self.update_catalog(ConfigMap(**self._config))
                for metric in metrics:
                    sys.stdout.write("\t{}\n".format(metric.namespace))

            sys.stdout.write("Printing metric catalog took {}\n\n".format(print_timer.elapsed()))
            sys.stdout.flush()

            # apply config to metrics for collection
            for metric in metrics:
                metric_config = self._config.copy()
                # search for default values matching metric's namespace
                for (ns, default) in defaults:
                    match = True
                    for i in range(len(ns)):
                        if ns[i] != metric.namespace[i].value:
                            match = False
                            break
                    # apply default value if no config entry is present
                    if match and default[0] not in metric_config:
                        metric_config[default[0]] = default[1]

                metric.config = metric_config

            # collected metrics
            with print_timer:
                sys.stdout.write("Metrics that can be collected right now are:\n")
                metrics_table = []
                metrics = self.collect(metrics)
                for metric in metrics:
                    metrics_table.append([metric.namespace, metric.data_type, metric.data])

                headers = ["NAMESPACE", "TYPE", "VALUE"]
                sys.stdout.write(_tabulate(metrics_table, headers))

            sys.stdout.write("Printing collected metrics took {}\n\n".format(print_timer.elapsed()))

            # contact us
            sys.stdout.write("Thank you for using this Snap plugin. If you have questions or are running\n"
                             "into errors, please contact us on Github (github.com/intelsdi-x/snap) or\n"
                             "our Slack channel (intelsdi-x.herokuapp.com).\n"
                             "The repo for this plugin can be found: github.com/intelsdi-x/<plugin-name>.\n"
                             "When submitting a new issue on Github, please include this diagnostic\n"
                             "print out so that we have a starting point for addressing your question.\n"
                             "Thank you.\n\n")

        sys.stdout.write("Printing diagnostic took {}\n\n".format(diagnostics_timer.elapsed()))
        sys.stdout.flush()

    def _parse_policy_namespaces(self, policy, key_type):
        """Returns list of keys with their info from all namespaces present in a given policy"""
        entries = []
        required_configs_missing = []
        defaults = []
        for namespace in policy.keys():
            for key in policy[namespace].rules:
                rls = policy[namespace].rules[key]
                mini, maxi, default = "", "", ""
                if hasattr(rls, "has_min") and rls.has_min is True:
                    mini = rls.minimum
                if hasattr(rls, "has_max") and rls.has_max is True:
                    maxi = rls.maximum
                if rls.has_default is True:
                    default = rls.default
                    # preserve default values with their namespace so they can be used to populate metric's config
                    defaults.append((tuple(policy[namespace].key), (key, default)))

                entries.append([namespace, key, key_type, rls.required, default, mini, maxi])

                if rls.required and key not in self._config:
                    required_configs_missing.append(key)

        return entries, required_configs_missing, defaults

    @abstractmethod
    def get_config_policy(self):
        """Returns the config policy for a plugin.

        The config policy for a plugin includes config value types (Integer,
        (Float, String and Bool) and rules which includes default values and
        min and max constraints for Float and Integery value types.

        Args:
            None

        Returns:
            :py:class:`snap_plugin.v1.config_policy.ConfigPolicy`

        """
        return GetConfigPolicyReply()


def _monitor(last_ping, stop_plugin, is_shutting_down, timeout=5):
    """Monitors health checks (pings) from the Snap framework.

    If the plugin doesn't receive 3 consecutive health checks from Snap the
    plugin will shutdown.  The default timeout is set to 5 seconds.
    """
    _timeout_count = 0
    _last_check = time.time()
    _sleep_interval = 1
    # set _sleep_interval if less than the timeout
    if timeout < _sleep_interval:
        _sleep_interval = timeout
    while True:
        time.sleep(_sleep_interval)
        # Signals that stop_plugin has been called
        if is_shutting_down():
            return
        # have we missed a ping during the last timeout duration
        if ((time.time() - _last_check) > timeout) and ((time.time() - last_ping()) > timeout):
            # reset last_check
            _last_check = time.time()
            _timeout_count += 1
            LOG.warning("Missed ping health check from the framework. " +
                        "({} of 3)".format(_timeout_count))
            if _timeout_count >= 3:
                stop_plugin()
                return
        elif (time.time() - last_ping()) <= timeout:
            _timeout_count = 0


def _tabulate(rows, headers, spacing=5):
    """Prepare simple table with spacing based on content"""
    if len(rows) == 0:
        return "None\n"
    assert len(rows[0]) == len(headers)
    count = len(rows[0])
    widths = [0 for _ in range(count)]
    rows = [headers] + rows

    for row in rows:
        for index, field in enumerate(row):
            if len(str(field)) > widths[index]:
                widths[index] = len(str(field))

    output = ""
    for row in rows:
        for index, field in enumerate(row):
            field = str(field)
            output += field + (widths[index] - len(field) + spacing) * " "
        output += "\n"
    return output
