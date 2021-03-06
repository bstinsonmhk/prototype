import contextlib
import json
import pathlib
import shutil
import subprocess
import time

from attr import attributes, attrib
from hamcrest import assert_that, equal_to, less_than_or_equal_to

import requests

##############################
# General utilities
##############################
_TEST_DIR = pathlib.Path(__file__).parent.parent
_REPO_DIR = _TEST_DIR.parent

# Command execution helper
def _run_command(cmd, work_dir, ignore_errors):
    print("  Running {} in {}".format(cmd, work_dir))
    output = None
    try:
        output = subprocess.check_output(
            cmd, cwd=work_dir, stderr=subprocess.PIPE
        ).decode()
    except subprocess.CalledProcessError as exc:
        output = exc.output.decode()
        if not ignore_errors:
            print("=== stdout for failed command ===")
            print(output)
            print("=== stderr for failed command ===")
            print(exc.stderr.decode())
            raise
    return output


##############################
# Local VM management
##############################

_VM_HOSTNAME_PREFIX = "leapp-tests-"
_VM_DEFS = {
    _VM_HOSTNAME_PREFIX + path.name: str(path)
        for path in (_TEST_DIR / "vmdefs").iterdir()
}

class VirtualMachineHelper(object):
    """Test step helper to launch and manage VMs

    Currently based specifically on local Vagrant VMs
    """

    def __init__(self):
        self.machines = {}
        self._resource_manager = contextlib.ExitStack()

    def ensure_local_vm(self, name, definition, destroy=False):
        """Ensure a local VM exists based on the given definition

        *name*: name used to refer to the VM in scenario steps
        *definition*: directory name in integration-tests/vmdefs
        *destroy*: whether or not to destroy any existing VM
        """
        hostname = _VM_HOSTNAME_PREFIX + definition
        if hostname not in _VM_DEFS:
            raise ValueError("Unknown VM image: {}".format(definition))
        if destroy:
            self._vm_destroy(hostname)
        self._vm_up(name, hostname)
        if destroy:
            self._resource_manager.callback(self._vm_destroy, name)
        else:
            self._resource_manager.callback(self._vm_halt, name)

    def get_hostname(self, name):
        """Return the expected hostname for the named machine"""
        return self.machines[name]

    def close(self):
        """Halt or destroy all created VMs"""
        self._resource_manager.close()

    @staticmethod
    def _run_vagrant(hostname, *args, ignore_errors=False):
        # TODO: explore https://pypi.python.org/pypi/python-vagrant
        vm_dir = _VM_DEFS[hostname]
        cmd = ["vagrant"]
        cmd.extend(args)
        return _run_command(cmd, vm_dir, ignore_errors)

    def _vm_up(self, name, hostname):
        result = self._run_vagrant(hostname, "up", "--provision")
        print("Started {} VM instance".format(hostname))
        self.machines[name] = hostname
        return result

    def _vm_halt(self, name):
        hostname = self.machines.pop(name)
        result = self._run_vagrant(hostname, "halt", ignore_errors=True)
        print("Suspended {} VM instance".format(hostname))
        return result

    def _vm_destroy(self, name):
        hostname = self.machines.pop(name)
        result = self._run_vagrant(hostname, "destroy", ignore_errors=True)
        print("Destroyed {} VM instance".format(hostname))
        return result


##############################
# Leapp commands
##############################

_LEAPP_SRC_DIR = _REPO_DIR / "src"
_LEAPP_BIN_DIR = _REPO_DIR / "bin"
_LEAPP_TOOL = str(_LEAPP_BIN_DIR / "leapp-tool")

_SSH_IDENTITY = str(_REPO_DIR / "integration-tests/config/leappto_testing_key")

def _install_client():
    """Install the CLI and its dependencies into a Python 2.7 environment"""
    py27 = shutil.which("python2.7")
    base_cmd = ["pipsi", "--bin-dir", str(_LEAPP_BIN_DIR)]
    if pathlib.Path(_LEAPP_TOOL).exists():
        # For some reason, `upgrade` returns 1 even though it appears to work
        # so we instead do a full uninstall/reinstall before the test run
        uninstall = base_cmd + ["uninstall", "--yes", "leappto"]
        _run_command(uninstall, work_dir=str(_REPO_DIR), ignore_errors=False)
    install = base_cmd + ["install", "--python", py27, str(_LEAPP_SRC_DIR)]
    print(_run_command(install, work_dir=str(_REPO_DIR), ignore_errors=False))

@attributes
class MigrationInfo(object):
    """Details of local hosts involved in an app migration command

    *local_vm_count*: Total number of local VMs found during migration
    *source_ip*: host accessible IP address found for source VM
    *target_ip*: host accessible IP address found for target VM
    """
    local_vm_count = attrib()
    source_ip = attrib()
    target_ip = attrib()

    @classmethod
    def from_vm_list(cls, machines, source_host, target_host):
        """Build a result given a local VM listing and migration hostnames"""
        vm_count = len(machines)
        source_ip = target_ip = None
        for machine in machines:
            if machine["hostname"] == source_host:
                source_ip = machine["ip"][0]
            if machine["hostname"] == target_host:
                target_ip = machine["ip"][0]
            if source_ip is not None and target_ip is not None:
                break
        return cls(vm_count, source_ip, target_ip)


class ClientHelper(object):
    """Test step helper to invoke the LeApp CLI

    Requires a VirtualMachineHelper instance
    """

    def __init__(self, vm_helper):
        self._vm_helper = vm_helper

    def redeploy_as_macrocontainer(self, source_vm, target_vm):
        """Recreate source VM as a macrocontainer on given target VM"""
        vm_helper = self._vm_helper
        source_host = vm_helper.get_hostname(source_vm)
        target_host = vm_helper.get_hostname(target_vm)
        self._convert_vm_to_macrocontainer(source_host, target_host)
        return self._get_migration_host_info(source_host, target_host)

    def check_response_time(self, cmd_args, time_limit):
        """Check given command completes within the specified time limit

        Returns the contents of stdout as a string.
        """
        start = time.monotonic()
        cmd_output = self._run_leapp(*cmd_args)
        response_time = time.monotonic() - start
        assert_that(response_time, less_than_or_equal_to(time_limit))
        return cmd_output

    @staticmethod
    def _run_leapp(*args):
        cmd = ["sudo", _LEAPP_TOOL]
        cmd.extend(args)
        return _run_command(cmd, work_dir=str(_LEAPP_BIN_DIR), ignore_errors=False)

    @classmethod
    def _convert_vm_to_macrocontainer(cls, source_host, target_host):
        result = cls._run_leapp(
            "migrate-machine",
            "--identity", _SSH_IDENTITY,
            "-t", target_host, source_host
        )
        msg = "Redeployed {} as macrocontainer on {}"
        print(msg.format(source_host, target_host))
        return result

    @classmethod
    def _get_migration_host_info(cls, source_host, target_host):
        leapp_output = cls._run_leapp("list-machines", "--shallow")
        machines = json.loads(leapp_output)["machines"]
        return MigrationInfo.from_vm_list(machines, source_host, target_host)


##############################
# Service status checking
##############################

class RequestsHelper(object):
    """Test step helper to check HTTP responses"""

    @classmethod
    def get_response(cls, service_url, wait_for_connection=None):
        """Get HTTP response from given service URL

        Responses are returned as requests.Response objects

        *service_url*: the service URL to query
        *wait_for_connection*: number of seconds to wait for a HTTP connection
                               to the service. `None` indicates that a response
                               is expected immediately.
        """
        deadline = time.monotonic()
        if wait_for_connection is None:
            fail_msg = "No response from service"
        else:
            fail_msg = "No response from service within {} seconds".format(wait_for_connection)
            deadline += wait_for_connection
        while True:
            try:
                return requests.get(service_url)
            except Exception:
                pass
            if time.monotonic() >= deadline:
                break
        raise AssertionError(fail_msg)

    @classmethod
    def get_responses(cls, urls_to_check):
        """Check responses from multiple given URLs

        Each URL can be either a string (which will be expected to return
        a response immediately), or else a (service_url, wait_for_connection)
        pair, which is interpreted as described for `get_response()`.

        Response are returned as a dictionary mapping from the service URLs
        to requests.Response objects.
        """
        # TODO: Use concurrent.futures to check the given URLs in parallel
        responses = {}
        for url_to_check in urls_to_check:
            if isinstance(url_to_check, tuple):
                url_to_check, wait_for_connection = url_to_check
            else:
                wait_for_connection = None
            responses[url_to_check] = cls.get_response(url_to_check,
                                                       wait_for_connection)
        return responses

    @classmethod
    def compare_redeployed_response(cls, original_ip, redeployed_ip, *,
                                    tcp_port, status, wait_for_target):
        """Compare a pre-migration app response with a redeployed response

        Expects an immediate response from the original IP, and allows for
        a delay before the redeployment target starts returning responses
        """
        # Get response from source VM
        original_url = "http://{}:{}".format(original_ip, tcp_port)
        original_response = cls.get_response(original_url)
        print("Response received from {}".format(original_url))
        original_status = original_response.status_code
        assert_that(original_status, equal_to(status), "Original status")
        # Get response from target VM
        redeployed_url = "http://{}:{}".format(redeployed_ip, tcp_port)
        redeployed_response = cls.get_response(redeployed_url, wait_for_target)
        print("Response received from {}".format(redeployed_url))
        # Compare the responses
        assert_that(redeployed_response.status_code, equal_to(original_status), "Redeployed status")
        original_data = original_response.text
        redeployed_data = redeployed_response.text
        assert_that(redeployed_data, equal_to(original_data), "Same response")


##############################
# Test execution hooks
##############################

# The @skip support here is based on:
# http://stackoverflow.com/questions/36482419/how-do-i-skip-a-test-in-the-behave-python-bdd-framework/42721605#42721605
_AUTOSKIP_TAG = "skip"
_WIP_TAG = "wip"
def _skip_test_group(context, test_group):
    """Decides whether or not to skip a test feature or test scenario

    Test groups are skipped if they're tagged with `@skip` and the `skip` tag
    is not explicitly requested through the command line options.

    The `--wip` option currently overrides `--tags`, so groups tagged with both
    `@skip` *and* `@wip` are also executed when the `wip` tag is requested.
    """
    active_tags = context.config.tags
    autoskip_tag_set = _AUTOSKIP_TAG in test_group.tags
    autoskip_tag_requested = active_tags and active_tags.check([_AUTOSKIP_TAG])
    wip_tag_set = _WIP_TAG in test_group.tags
    wip_tag_requested = active_tags and active_tags.check([_AUTOSKIP_TAG, _WIP_TAG])
    override_autoskip = autoskip_tag_requested or (wip_tag_set and wip_tag_requested)
    skip_group = autoskip_tag_set and not override_autoskip
    if skip_group:
        test_group.skip("Marked with @skip")
    return skip_group

def before_all(context):
    # Some steps require sudo, so for convenience in interactive use,
    # we ensure we prompt for elevated permissions immediately,
    # rather than potentially halting midway through a test
    subprocess.check_output(["sudo", "echo", "Elevated permissions needed"])

    # Install the CLI for use in the tests
    _install_client()

    # Use contextlib.ExitStack to manage global resources
    context._global_cleanup = contextlib.ExitStack()

def before_feature(context, feature):
    if _skip_test_group(context, feature):
        return

    # Use contextlib.ExitStack to manage per-feature resources
    context._feature_cleanup = contextlib.ExitStack()

def before_scenario(context, scenario):
    if _skip_test_group(context, scenario):
        return

    # Each scenario has a contextlib.ExitStack instance for resource cleanup
    context.scenario_cleanup = contextlib.ExitStack()

    # Each scenario gets a VirtualMachineHelper instance
    # VMs are relatively slow to start/stop, so by default, we defer halting
    # or destroying them to the end of the overall test run
    # Feature steps can still opt in to eagerly cleaning up a scenario's VMs
    # by doing `context.scenario_cleanup.callback(context.vm_helper.close)`
    context.vm_helper = vm_helper = VirtualMachineHelper()
    context._global_cleanup.callback(vm_helper.close)

    # Each scenario gets a ClientHelper instance
    context.cli_helper = cli_helper = ClientHelper(context.vm_helper)

    # Each scenario gets a RequestsHelper instance
    context.http_helper = RequestsHelper()

def after_scenario(context, scenario):
    if scenario.status == "skipped":
        return
    context.scenario_cleanup.close()

def after_feature(context, feature):
    if feature.status == "skipped":
        return
    context._feature_cleanup.close()

def after_all(context):
    context._global_cleanup.close()
