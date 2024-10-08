"""
Note: Unlike the rest of this project, this file is under GPL-3.0 license
The LinkInjector class is copied from Cotroneo et al.s paper
 "ThorFI: A Novel Approach for Network Fault Injection as a Service.”
  https://doi.org/10.1016/j.jnca.2022.103334

The following changes have been made to this class in the course of thesis work:
- Adaptation of logging to integrate with mininets logging
- Adaption of injection commands to work with mininet
- Addition of tags for logging of faults
- Implementation of a new "redirect" fault, both for single protocols, and for all traffic
- Removal of injecting more than one fault per Injector Instance
- Removal of OpenStack specific code
- Removal of code related to calling the injector via the network
- Improvements in documentation
- Minor refactors

For more details, the file thorfi_injector/injector_agent.py in commit db24eccf7796b16ec3f4b2f74df9877cb7b2e2df
contains the unmodified ThorFI file, and can be diffed against the current version.

Code where development was supported by other developers is explicitly commented.
"""
import asyncio
import json
import re
import subprocess
import pathlib
import time
import math

from mininet import log

from subprocess import call, run

from mininet.faultlogger import FaultLogger

class MultiInjector:
    """Link-based injector. Injects multiple faults into a link. All faults need to be based on the tc-netem module.
    """
    def __init__(self,
                 target_namespace_pid=None,  # pid of the main node shell we should inject on
                 tag=None,  # Must be unique between all faults

                 fault_pattern=None,  # user-provided, "burst", "degradation", "random", "persistent"
                 fault_pattern_args=None,  # user-provided
                 config_string=None,

                 pre_injection_time=0,  # Time we wait before the injection activates
                 injection_time=20,  # How long the injection activates
                 post_injection_time=0): # How long after the injection we wait until the injector considers itself inactive


        self.target_process_pid = target_namespace_pid
        self.tag = tag

        self.fault_pattern = fault_pattern
        if not isinstance(fault_pattern_args, list) and fault_pattern_args is not None:
            log.warn("fault_patterns are not a list, are you sure that is what you want?\n")
        self.fault_pattern_args = fault_pattern_args



        self.pre_injection_time = pre_injection_time
        self.injection_time = injection_time
        self.post_injection_time = post_injection_time

        self.inject_command, self.eject_command = self.build_start_command(config_string)


    def build_start_command(self, config_string):
        start_command = f"echo '{config_string}' | tcset /dev/stdin --import-setting"

        config = json.loads(config_string)
        target_interface_names = list(config.keys())

        unset_config = {}

        reset_single_interface_config = {"outgoing": {},
                        "incoming": {}}

        for interface_name in target_interface_names:
            unset_config[interface_name] = reset_single_interface_config

        reset_config_string = json.dumps(unset_config)

        end_command = f"echo '{reset_config_string}' | tcset /dev/stdin --import-setting"
        return start_command, end_command


    async def go(self):
        await self.do_injection()


    def execute_command_for_node(self, pid_of_node, command_to_execute, enable):
        """Executes the command on the node. Enable signals whether the command
        is activating or deactivating a fault, which is important for logging.
        If command_to_execute is None, no command is executed, but the information
        is still passed to the logger"""

        base_command = f"nsenter --target {str(pid_of_node)} --net --pid "


        full_command = base_command + command_to_execute

        if '|' in full_command:
            full_command = full_command.replace("|", f"nsenter --target {str(pid_of_node)} --net --pid ")

        time_before = time.time()
        retcode = run(full_command, shell=True).returncode
        time_after = time.time()

        if enable:
            FaultLogger.set_fault_active(self.tag, "multi-fault", command_to_execute, retcode)
        else:
            FaultLogger.set_fault_inactive(self.tag)

        if retcode < 0:
                log.debug("Command '%s' was terminated by signal %s\n" % (full_command, -retcode))
        elif retcode != 0:
            log.debug("Command '%s' did not terminate correctly (retcode %s)\n" % (full_command, retcode))
        else:
            log.debug("Command '%s' terminated correctly (retcode %s)\n" % (full_command, retcode))


    async def _inject_burst(self):
        log.info("Fault %s commencing burst\n" % (self.tag))

        burst_config = self.fault_pattern_args
        if len(burst_config) < 2:
            log.error(f"{self.tag} burst is missing parameters, defaulting to 1 second per 2 seconds\n")
            burst_duration = 1
            burst_period = 2
        else:
            burst_duration = int(burst_config[0]) / 1000
            burst_period = int(burst_config[1]) / 1000  # after each burst, wait for (burst_period - burst_duration)

        burst_num = int((self.injection_time) / burst_period)  # how often we burst

        for _ in range(burst_num):
            self.execute_command_for_node(self.target_process_pid, self.inject_command, True)
            await asyncio.sleep(burst_duration)
            self.execute_command_for_node(self.target_process_pid, self.eject_command, False)
            await asyncio.sleep(burst_period - burst_duration)

    async def _inject_persistent(self):
        log.info("Fault %s commencing persistent\n" % (self.tag))
        duration_in_seconds = self.injection_time
        self.execute_command_for_node(self.target_process_pid, self.inject_command, True)
        await asyncio.sleep(duration_in_seconds)
        self.execute_command_for_node(self.target_process_pid, self.eject_command, False)

    async def do_injection(self):
        log.info("Fault %s waits %s s of pre-injection time\n" % (self.tag, self.pre_injection_time))
        await asyncio.sleep(self.pre_injection_time)
        if self.fault_pattern == 'burst':
            await self._inject_burst()
        elif self.fault_pattern == 'persistent':
            await self._inject_persistent()
        else:
            log.error(f"{self.tag} has unknown fault pattern: {self.fault_pattern}\n")

        log.info("Fault %s waits %s s of post-injection time\n" % (self.tag, self.post_injection_time))
        await asyncio.sleep(self.post_injection_time)

        return


class LinkInjector:
    """Link-based injector. Injects faults into links by modifying their corresponding interfaces, e.g. with tc-netem.
    """

    def __init__(self,
                 target_interface=None,  #  Interface name, like eth0.
                 target_namespace_pid=None,  # pid of the main node shell we should inject on
                 tag=None, # Must be unique between all faults
                 fault_target_protocol=None, # optional, if fault target is not any (or 'any') generate cmds for injecting according to protocol
                 # and port number
                 fault_target_dst_ports=None,  # optional, int representing port number. Fault applies to this port only.
                 fault_target_src_ports=None,  # optional, int representing port number. Fault applies to this port only.
                 fault_type=None,  # "delay", "loss", "corrupt", "duplicate", "reorder", "down", "redirect", "bottleneck"
                 fault_pattern=None,  # user-provided, "burst", "degradation", "random", "persistent"
                 fault_pattern_args=None,  # user-provided
                 fault_args=None,  # user-provided: how harsh failure is, depends on fault_pattern
                 pre_injection_time=0, # Time we wait before the injection activates
                 injection_time=20, # How long the injection activates
                 post_injection_time=0): # How long after the injection we wait until the injector considers itself inactive


        # target_nics: is a list of network resources to be injected
        # fault: is the fault to be injected in the network resource
        # time: describe how last the injection

        self.target_interface = target_interface
        self.namespace_pid = target_namespace_pid
        self.tag = tag

        self.fault_target_protocol = fault_target_protocol
        self.fault_target_dst_ports = fault_target_dst_ports
        self.fault_target_src_ports = fault_target_src_ports

        self.fault_type = fault_type
        self.fault_pattern = fault_pattern

        if not isinstance(fault_pattern_args, list) and fault_pattern_args is not None:
            log.warn("fault_pattern_args are not a list, are you sure that is what you want?\n")
        self.fault_pattern_args = fault_pattern_args

        if not isinstance(fault_args, list) and fault_args is not None:
            log.warn("fault_args are not a list, are you sure that is what you want?\n")
        self.fault_args = fault_args

        self.pre_injection_time = pre_injection_time
        self.injection_time = injection_time
        self.post_injection_time = post_injection_time

        self.target_protocol_table = {
            'ICMP': '1',
            'IGMP': '2',
            'IP': '4',
            'TCP': '6',
            'UDP': '17',
            'IPv6': '41',
            'IPv6-ICMP': '58'
        }

    def getFaultType(self):
        return self.fault_type

    def getFaultPattern(self):
        return self.fault_pattern

    def getPreInjectionTime(self):
        return float(self.pre_injection_time)

    def getInjectionTime(self):
        return float(self.injection_time)

    def getPostInjectionTime(self):
        return float(self.post_injection_time)

    async def _inject_burst_pattern(self):
        # This uses nc s "persistent" , and turns it on/off, as often as the burst requires
        if len(self.fault_pattern_args) < 2:
            log.error(f"{self.tag} Burst doesn't have enough arguments to be defined")
        burst_config = self.fault_pattern_args
        burst_duration = float(burst_config[0]) / 1000
        burst_period = float(burst_config[1]) / 1000

        log.info("Fault %s starting burst injections, time: %s\n" % (self.tag, self.getInjectionTime()))
        burst_num = int((self.getInjectionTime()) / burst_period)

        log.debug("Burst config: burst_duration: %s burst_period: %s burst_num: %s\n" % (
            burst_duration, burst_period, burst_num))

        for _ in range(burst_num):
            # iterate over all target devices to enable injection
            interface = self.target_interface
            log.debug("%s BURST ENABLE injection on nic %s\n" % (self.tag, interface))
            self.inject_nics(interface, self.namespace_pid, self.getFaultType(), 'persistent', [''],
                             self.fault_args,
                             self.fault_target_protocol,
                             self.fault_target_dst_ports, self.fault_target_src_ports, True)

            log.debug("%s WAIT BURST DURATION...%s\n" % (self.tag, burst_duration))
            await asyncio.sleep(burst_duration)

            log.debug("%s BURST DISABLE injection on nic %s \n" % (self.tag, interface))
            self.inject_nics(interface, self.namespace_pid, self.getFaultType(), 'persistent', [''],
                             self.fault_args,
                             self.fault_target_protocol,
                             self.fault_target_dst_ports, self.fault_target_src_ports, False)

            log.debug("%s WAIT BURST remaining time...%s\n" % (self.tag, (burst_period - burst_duration)))
            await asyncio.sleep(burst_period - burst_duration)

    async def _inject_degradation_pattern(self):
        # This uses nc s "random" pattern
        # increment for 'fault_pattern_args' each second

        if len(self.fault_pattern_args) >= 4:
            end_degradation = int(self.fault_pattern_args[3])
            end_degradation = min(end_degradation, 100)
        else:
            end_degradation = 100
        if len(self.fault_pattern_args) >= 3:
            start_degradation = str(self.fault_pattern_args[2])
        else:
            start_degradation = 0
        if len(self.fault_pattern_args) >= 2:
            degradation_step_length = int(self.fault_pattern_args[1]) / 1000
        else:
            degradation_step_length = 1000 / 1000

        if len(self.fault_pattern_args) >= 1:
            degradation_step_size = str(self.fault_pattern_args[0])
        else:
            degradation_step_size = str(5)
            log.error(f"{self.tag} does not have enough pattern_args to define degradation step, defaulting to 5")

        degradation_value = start_degradation
        number_of_steps = int(self.getInjectionTime() / degradation_step_length)

        interface = self.target_interface

        log.info("Fault %s starting degradation with %s perc/s\n" % (self.tag, degradation_value))

        for i in range(number_of_steps):
            log.debug("%s #%s step..." % (self.tag, i))
            # iterate over all target devices to enable injection

            log.debug("%s DEGRADATION ENABLE injection on nic %s \n" % (self.tag, interface))

            self.inject_nics(interface, self.namespace_pid, self.getFaultType(), 'random', [degradation_value],
                             self.fault_args,
                             self.fault_target_protocol, self.fault_target_dst_ports,
                             self.fault_target_src_ports, True)

            log.debug("%s WAIT DEGRADATION DURATION...%s\n" % (self.tag, degradation_step_length))
            await asyncio.sleep(degradation_step_length)

            log.debug("%s DEGRADATION DISABLE injection on nic %s\n" % (self.tag, interface))
            self.inject_nics(interface, self.namespace_pid, self.getFaultType(), 'random', [degradation_value],
                             self.fault_args,
                             self.fault_target_protocol, self.fault_target_dst_ports,
                             self.fault_target_src_ports, False)

            degradation_value = str(int(degradation_value) + int(degradation_step_size))
            if int(degradation_value) > end_degradation:
                degradation_value = str(end_degradation)

            log.debug("%s updated degradation value %s\n" % (self.tag, degradation_value))

    async def _inject_persistent_or_random_pattern(self):
        # iterate over all target devices to enable injection
        interface = self.target_interface
        log.info("Fault %s starting %s injection on nic %s\n" % (self.tag, self.getFaultPattern(), interface))

        self.inject_nics(interface, self.namespace_pid, self.getFaultType(), self.getFaultPattern(),
                         self.fault_pattern_args, self.fault_args,
                         self.fault_target_protocol, self.fault_target_dst_ports,
                         self.fault_target_src_ports, True)

        log.debug("%s Wait the injection time (%s s)\n" % (self.tag, self.getInjectionTime()))
        await asyncio.sleep(self.getInjectionTime())

        # iterate over all target devices to disable injection
        log.debug("%s Disable injection on nic %s\n" % (self.tag, interface))

        self.inject_nics(interface, self.namespace_pid, self.getFaultType(), self.getFaultPattern(),
                         self.fault_pattern_args, self.fault_args,
                         self.fault_target_protocol, self.fault_target_dst_ports,
                         self.fault_target_src_ports, False)

    async def go(self):
        await self.do_injection()

    async def do_injection(self):
        log.info("Fault %s waits %s s of pre-injection time\n" % (self.tag, self.getPreInjectionTime()))
        await asyncio.sleep(self.getPreInjectionTime())

        if 'burst' in self.getFaultPattern():
            await self._inject_burst_pattern()
        elif 'degradation' in self.getFaultPattern():
            await self._inject_degradation_pattern()
        else:
            await self._inject_persistent_or_random_pattern()

        # END INJECTION CODE

        # wait 'self.post_injection_time' after removing injection
        log.info("Fault %s waits %s s of post-injection time\n" % (self.tag, self.getPostInjectionTime()))
        await asyncio.sleep(self.getPostInjectionTime())

    def make_nics_injection_command(self, node_pid, device, fault_type, fault_pattern, fault_pattern_args, fault_args,
                                    tc_cmd):
        log.debug(
            "[make_nics_injection_command] CONFIG: device %s, fault_type %s, fault_pattern %s, fault_pattern_args %s, fault_args %s, tc_cmd %s\n"
            % (device, fault_type, fault_pattern, fault_pattern_args, fault_args, tc_cmd))

        if node_pid is None:
            # Node is not in a network namespace, so base command doesn't need to enter a namespace
            base_command = ''
        else:
            base_command = 'nsenter --target ' + str(node_pid) + ' --net '

        base_qdisc_netem_command = base_command + 'tc qdisc ' + tc_cmd + ' dev ' + device + ' root netem '
        # base command is not used for redirects, since those don't use tc netem
        command = None

        # NOTE: for NODE_DOWN and NIC_DOWN fault type does not make sense the random fault_pattern.
        #       we implement only a persistent flavor

        if 'random' in fault_pattern:
            if 'delay' in fault_type:
                # e.g., tc qdisc add dev tap0897f3c6-e0 root netem delay 50 reorder 50%
                # Unit for 50 is ms, but according to docs the ms doesn't need to be passed
                random_perc = 100 - int(fault_pattern_args[0])
                command = base_qdisc_netem_command + fault_type + ' ' + fault_args[0] + ' reorder ' + str(
                    random_perc) + '%'
            elif 'redirect' in fault_type:
                percentage_to_redirect = int(fault_pattern_args[0]) # assume 10 for 10%
                # Our source of random is a random 32 bit value - so calculate a number which if above n percent of that
                maximum_value = 4294967295
                boundary = int(maximum_value * (percentage_to_redirect / 100))

                destination_interface = fault_args[0]
                try:
                    redirect_or_mirror = fault_args[1]
                    if redirect_or_mirror not in ['mirror', 'redirect']:
                        raise IndexError
                except IndexError:
                    redirect_or_mirror = 'redirect'
                # We'll only ever have one of these per interface, so this static handle is fine
                # This follows the man page examples
                # add ingress qdisc first
                if 'add' in tc_cmd:
                    # This value is 32 bit unsigned, even though in output values greater than 2147483647 become unsigned
                    filter_string = f' basic match "meta( random mask {maximum_value} lt {boundary} ) " ' # Felix Gohla found out that this line requires an additional mask.
                    # The reason for that is that the "random" value is 64 bits of random (which contradicts the manpage,
                    # which states that it's 32 bits of random, and the value to compare is truncated to 32 bits.
                    # (So without the mask theres ~32 bits that will always be greater than the value provided by a caller)


                    # ingress qdiscs don't seem to respect their assigned handle, they always fall back to ffff, so
                    # change at your own risk
                    prep_command = base_command + 'tc qdisc ' + tc_cmd + ' dev ' + device + ' handle ffff: ingress '
                    command = base_command + 'tc filter ' + tc_cmd + ' dev ' + device + f' parent ffff: {filter_string} ' + \
                        ' action mirred egress ' + redirect_or_mirror + ' dev ' + destination_interface  # get parent
                    command = prep_command + " ; " + command
                elif 'del' in tc_cmd:
                    command = base_command + 'tc qdisc ' + tc_cmd + ' dev ' + device + ' ingress '
            else:
                # in that case for corruption and loss we can use the 'fault_args' that already include random probability
                command = base_qdisc_netem_command + fault_type + ' ' + str(fault_pattern_args[0]) + '%'

        elif 'persistent' in fault_pattern:
            # Persistent fault type means setting a probability to 100%. For delay injection we can just use the default usage for 'delay' fault type
            if 'delay' in fault_type:
                command = base_qdisc_netem_command + fault_type + ' ' + fault_args[0]
            elif 'bottleneck' in fault_type:
                # the command is like: tc qdisc add dev tapa68bfef8-df root tbf rate 256kbit burst 1600 limit 3000
                default_bottleneck_burst = '1600'
                default_limit_burst = '3000'
                if len(fault_args) > 2:
                    default_bottleneck_burst = str(fault_args[1])
                    default_limit_burst = str(fault_args[2])
                elif len(fault_args) == 2:
                    default_bottleneck_burst = str(fault_args[1])
                command = (base_command + 'tc qdisc ' + tc_cmd + ' dev '
                           + device + ' root tbf rate ' + fault_args[
                               0] + 'kbit burst ' + default_bottleneck_burst + ' limit ' + default_limit_burst)
            elif 'redirect' in fault_type:
                destination_interface = fault_args[0]
                try:
                    redirect_or_mirror = fault_args[1]
                    if redirect_or_mirror not in ['mirror', 'redirect']:
                        raise IndexError
                except IndexError:
                    redirect_or_mirror = 'redirect'

                # We'll only ever have one of these per interface, so this static handle is fine
                # This follows the man page examples
                # add ingress qdisc first
                if 'add' in tc_cmd:
                    # ingress qdiscs don't seem to respect their assigned handle, they always fall back to ffff, so
                    # change at your own risk
                    prep_command = base_command + 'tc qdisc ' + tc_cmd + ' dev ' + device + ' handle ffff: ingress '
                    command = base_command + 'tc filter ' + tc_cmd + ' dev ' + device + ' parent ffff: matchall ' + \
                              ' action mirred egress ' + redirect_or_mirror + ' dev ' + destination_interface  # get parent
                    command = prep_command + " ; " + command
                elif 'del' in tc_cmd:
                    command = base_command + 'tc qdisc ' + tc_cmd + ' dev ' + device + ' ingress '

            elif 'down' in fault_type:
                if 'add' in tc_cmd:
                    command = base_command + 'ifconfig ' + device + ' down'
                elif 'del' in tc_cmd:
                    command = base_command + 'ifconfig ' + device + ' up'
            else:
                # in that case for corruption and loss we can use the 'fault_args' to set 100% probability
                command = base_qdisc_netem_command + fault_type + ' 100%'

        else:
            log.error("Fault pattern %s is unknown\n" % fault_pattern)
        return command

    def make_filtered_nics_injection_commands(self, node_pid, fault_pattern, fault_pattern_args, fault_type, fault_args,
                                              device,
                                              target_protocol,
                                              target_dst_ports=None, target_src_ports=None, enable=False):

        log.debug(
            "[make_filter_cmds] CONFIG: fault_pattern %s, fault_pattern_args %s, fault_type %s, fault_args %s, device %s, target_protocol %s, target_dst_ports %s, target_src_ports %s, enable %s\n"
            % (fault_pattern, fault_pattern_args, fault_type, fault_args, device, target_protocol, target_dst_ports,
               target_src_ports, enable))

        if node_pid is None:
            # Node is not in a network namespace, so base command doesn't need to enter a namespace
            base_command = ''
        else:
            base_command = 'nsenter --target ' + str(node_pid) + ' --net '

        if enable:
            if 'redirect' in fault_type:
                # We need to work with the ingress qdisc instead of the default one, so add that one instead
                # almost the same as prep_command from redirect of all protocols
                cmd_list = [base_command + 'tc qdisc add dev ' + device + ' handle ffff: ingress ']
                # For some reason using handle 1: ingress still leads to a ffff handle
                # (tc qdisc show dev s1-eth1 gives output of qdisc ingress ffff: parent ffff:fff1)
                # I'm not completely sure why?
            else:
                cmd_list = [base_command + 'tc qdisc add dev ' + device + ' root handle 1: prio']

            # The parent is wrong if fault_type is redirect. This will be fixed later
            base_qdisc_netem_command = base_command + 'tc filter add dev ' + device + ' parent 1:0 protocol ip prio 1 u32 '
            target_protocol_cmd = 'match ip protocol ' + self.target_protocol_table[target_protocol] + ' 0xff'

            # redirect needs no special case here, since these are added to the qdisc as defined by tag
            if target_dst_ports:
                for target_port in target_dst_ports:
                    target_port_cmd = 'match ip dport ' + str(target_port) + ' 0xffff'
                    cmd_list.append(
                        base_qdisc_netem_command + target_protocol_cmd + ' ' + target_port_cmd + ' flowid 1:1')
            if target_src_ports:
                for target_port in target_src_ports:
                    target_port_cmd = 'match ip sport ' + str(target_port) + ' 0xffff'
                    cmd_list.append(
                        base_qdisc_netem_command + target_protocol_cmd + ' ' + target_port_cmd + ' flowid 1:1')
            if target_dst_ports is None and target_src_ports is None:
                cmd_list.append(base_qdisc_netem_command + target_protocol_cmd + ' flowid 1:1')

            # enable fault injection
            if 'random' in fault_pattern:
                if 'delay' in fault_type:
                    # e.g., tc qdisc add dev tap0897f3c6-e0 root netem delay 50ms reorder 50%
                    random_perc = 100 - int(fault_pattern_args[0])
                    cmd_list.append(
                        base_command + 'tc qdisc add dev ' + device + ' parent 1:1 handle 2: netem ' + fault_type + ' ' +
                        fault_args[0] + ' reorder ' + str(
                            random_perc) + '%')
                else:
                    cmd_list.append(
                        base_command + 'tc qdisc add dev ' + device + ' parent 1:1 handle 2: netem ' + fault_type + ' ' + str(
                            fault_pattern_args[0]) + '%')
            elif 'persistent' in fault_pattern:
                if 'bottleneck' in fault_type:
                    # the command is like: tc qdisc add dev tapa68bfef8-df root tbf rate 256kbit burst 1600 limit 3000
                    default_bottleneck_burst = '1600'
                    default_limit_burst = '3000'
                    if len(fault_args) > 2:
                        default_bottleneck_burst = str(fault_args[1])
                        default_limit_burst = str(fault_args[2])
                    if len(fault_args) == 2:
                        default_bottleneck_burst = str(fault_args[1])
                    cmd_list.append(
                        base_command + 'tc qdisc add dev ' + device + ' parent 1:1 handle 2: tbf rate ' + fault_args[
                            0] + 'kbit burst ' + default_bottleneck_burst + ' limit ' + default_limit_burst)
                elif 'redirect' in fault_type:
                    destination_interface = fault_args[0]
                    try:
                        redirect_or_mirror = fault_args[1]
                        if redirect_or_mirror not in ['mirror', 'redirect']:
                            raise IndexError
                    except IndexError:
                        redirect_or_mirror = 'redirect'
                    # We modify the commands already in the cmd_list, and add the redirect action to them
                    append_string = ' action mirred egress ' + redirect_or_mirror + ' dev ' + destination_interface
                    new_cmd_list = []
                    for cmd in cmd_list:
                        if 'match' in cmd:
                            command_with_corrected_id = cmd.replace('parent 1:0', 'parent ffff:')
                            new_cmd_list.append(command_with_corrected_id + append_string)
                        else:
                            new_cmd_list.append(cmd)
                    cmd_list = new_cmd_list

                else:
                    if 'delay' in fault_type:
                        tc_arg = str(fault_args[0])
                    else:
                        tc_arg = '100%'
                    cmd_list.append(
                        base_command + 'tc qdisc add dev ' + device + ' parent 1:1 handle 2: netem ' + fault_type + ' ' + tc_arg)
        else:
            cmd_list = []
            if 'redirect' in fault_type:
                # We added a different queue, so we need to delete a different one
                cmd_list.append(base_command + 'tc qdisc del dev ' + device + ' ingress ')
            else:
                cmd_list.append(base_command + 'tc qdisc del dev ' + device + ' root handle 1: prio')

        log.debug("cmd_list generated => %s\n" % cmd_list)

        return cmd_list

    def inject_nics(self, device, node_pid, fault_type, fault_pattern, fault_pattern_args, fault_args,
                    target_protocol, target_dst_ports, target_src_ports, enable):

        # tc/netem commands used to inject fault
        #
        # fault_type = [ delay | redirect | bottleneck | down | loss | corrupt | duplicate | reorder ]
        # fault_args = [<latency>ms | <percentage>%]
        #
        # DELAY:
        # tc qdisc add dev <nic> root netem delay <latency>ms"
        #
        # LOSS:
        # tc qdisc add dev <nic> root netem loss <percentage>%
        #
        # CORRUPT:
        # tc qdisc change dev <nic> root netem corrupt <percentage>%

        # NOTE: to handle properly floating ip injection we need to filter on floating ip
        #
        # Example:
        #
        # to enable:
        # ip netns exec qrouter-8f998d26-79e1-41ff-8fd8-ba362ab4fc92 tc qdisc add dev qg-a931d750-88 root handle 1: prio
        # ip netns exec qrouter-8f998d26-79e1-41ff-8fd8-ba362ab4fc92 tc filter add dev qg-a931d750-88 parent 1:0 protocol ip prio 1 u32 match ip src 10.0.20.232 flowid 1:1
        # ip netns exec qrouter-8f998d26-79e1-41ff-8fd8-ba362ab4fc92 tc filter add dev qg-a931d750-88 parent 1:0 protocol ip prio 1 u32 match ip dst 10.0.20.232 flowid 1:1
        # ip netns exec qrouter-8f998d26-79e1-41ff-8fd8-ba362ab4fc92 tc qdisc add dev qg-a931d750-88 parent 1:1 handle 2: netem delay 1000ms
        #
        # to disable:
        # ip netns exec qrouter-8f998d26-79e1-41ff-8fd8-ba362ab4fc92 tc qdisc add dev qg-a931d750-88 root handle 1: prio

        # NOTE: We are injecting on the other network resources except floating IP
        # if fault target is any call make_nics_injection_command
        if 'any' in target_protocol:
            # Inject into all protocols
            if enable:
                # enable fault injection
                command = self.make_nics_injection_command(node_pid, device, fault_type, fault_pattern,
                                                           fault_pattern_args if fault_pattern_args else None,
                                                           fault_args, 'add')
            else:
                command = self.make_nics_injection_command(node_pid, device, fault_type, fault_pattern,
                                                           fault_pattern_args if fault_pattern_args else None,
                                                           fault_args, 'del')

            log.debug("Execute command in namespace for process  %s: '%s'\n" % (node_pid, command))
            retcode = call(command, shell=True)
            if enable:
                FaultLogger.set_fault_active(self.tag, self.fault_type, command, retcode)
            else:
                FaultLogger.set_fault_inactive(self.tag)

            if retcode < 0:
                log.debug("Command '%s' was terminated by signal %s\n" % (command, -retcode))
            elif retcode != 0:
                log.debug("Command '%s' did not terminate correctly (retcode %s)\n" % (command, retcode))
            else:
                log.debug("Command '%s' terminated correctly (retcode %s)\n" % (command, retcode))

        # if fault target is not any generate cmds for injecting according to protocol and port number
        else:
            # Inject into only specific protocols
            cmd_list = self.make_filtered_nics_injection_commands(node_pid, fault_pattern, fault_pattern_args,
                                                                  fault_type, fault_args,
                                                                  device,
                                                                  target_protocol, target_dst_ports,
                                                                  target_src_ports, enable)

            for command in cmd_list:
                log.debug("Execute command in namespace for process %s: '%s'\n" % (node_pid, command))

                retcode = call(command, shell=True)
                if enable:
                    FaultLogger.set_fault_active(self.tag, self.fault_type, command, retcode)
                else:
                    FaultLogger.set_fault_inactive(self.tag)

                if retcode < 0:
                    log.debug("Command '%s' was terminated by signal %s\n" % (command, -retcode))
                elif retcode != 0:
                    log.debug("Command '%s' did not terminate correctly (retcode %s)\n" % (command, retcode))
                else:
                    log.debug("Command '%s' terminated correctly (retcode %s)\n" % (command, retcode))


class NodeInjector:
    """Injector for injecting nodes. Nodes are identified by their process id, and faults are executed in the
    same cgroup/network namespace of the original process."""
    def __init__(self,
                 target_process_pid=None,  # This process id represents the "node" we want to run on
                 tag=None, # Must be unique between all faults
                 fault_type=None,  # "stress_cpu", "custom"
                 pre_injection_time=0, # Time we wait before the injection activates
                 injection_time=20, # How long the injection activates
                 post_injection_time=0, # How long after the injection we wait until the injector considers itself inactive
                 fault_args=None,  # For stress: Percentage. For custom: Start command/end command
                 fault_pattern=None,  # persistent|burst|degradation
                 fault_pattern_args=None  # intensity of pattern, etc.
                 ):
        self.target_process_pid = target_process_pid
        self.tag = tag
        self.fault_type = fault_type
        self.pre_injection_time = pre_injection_time
        self.injection_time = injection_time
        self.post_injection_time = post_injection_time
        self.fault_args = fault_args
        self.fault_pattern = fault_pattern
        self.fault_pattern_args = fault_pattern_args

        if fault_type == "stress_cpu":
            self.cpu_cgroup_name = self._get_cgroup_name()

    async def go(self):
        await self.do_injection()

    def _get_cgroup_name(self):
        # Returns name of the cgroup that the target process is running in
        pid = self.target_process_pid
        path_to_process_cgroup = f"/proc/{pid}/cgroup"
        cgroup_command = ["cat", path_to_process_cgroup]
        try:
            cgroup_process = subprocess.run(cgroup_command, text=True, capture_output=True)
            cgroup_process.check_returncode()
            cgroup_path = cgroup_process.stdout
            log.debug("cgroups for pid " + str(pid) + ": " + cgroup_path)
            cpu_cgroup_pattern = r"^(\d*):cpu,cpuacct:/(.*)$"
            match = re.search(cpu_cgroup_pattern, cgroup_path, re.MULTILINE)
            cgroup_name = match.group(2)
            return cgroup_name

        except subprocess.CalledProcessError:
            log.error(f"Can't access cgroups information for fault {self.tag}")
        return None

    def execute_command_for_node(self, pid_of_node, command_to_execute, enable):
        """Executes the command on the node. Enable signals whether the command
        is activating or deactivating a fault, which is important for logging.
        If command_to_execute is None, no command is executed, but the information
        is still passed to the logger"""
        if command_to_execute is None:
            if enable:
                FaultLogger.set_fault_active(self.tag, self.fault_type, "Dummy command, no action taken", 0)
            else:
                FaultLogger.set_fault_inactive(self.tag)
            return

        base_command = f"nsenter --target {str(pid_of_node)} --net --pid --cgroup "
        full_command = base_command + command_to_execute

        # Without this all commands post-pipe aren't executed in the namespace anymore
        if '|' in full_command:
            full_command = full_command.replace("|", f"nsenter --target {str(pid_of_node)} --net --pid --cgroup")


        time_before = time.time()
        retcode = run(full_command, shell=True).returncode
        time_after = time.time()
        if time_after - time_before > 2:
            log.error(
                "Node command took more than 2 seconds to execute. Blocking commands can lead to unexpected outcomes, like logs not generating!\n")
        if enable:
            FaultLogger.set_fault_active(self.tag, self.fault_type, command_to_execute, retcode)
        else:
            FaultLogger.set_fault_inactive(self.tag)

        if retcode < 0:
            log.debug("Command '%s' was terminated by signal %s\n" % (full_command, -retcode))
        elif retcode != 0:
            log.debug("Command '%s' did not terminate correctly (retcode %s)\n" % (full_command, retcode))
        else:
            log.debug("Command '%s' terminated correctly (retcode %s)\n" % (full_command, retcode))

    def _get_cgroup_size(self):
        size_command = ["cgget", "-g", "cpu", self.cpu_cgroup_name]
        try:
            cgroup_process = subprocess.run(size_command, text=True, capture_output=True)
            cgroup_process.check_returncode()
            cpu_cgroup_details = cgroup_process.stdout

            period_regex = r"^cpu\.cfs_period_us: (\d*)$"
            quota_regex = r"^cpu\.cfs_quota_us: (\d*)$"
            cpu_period = re.search(period_regex, cpu_cgroup_details, re.MULTILINE).group(1)
            cpu_quota = re.search(quota_regex, cpu_cgroup_details, re.MULTILINE).group(1)

            cpu_ratio = int(cpu_quota) / int(cpu_period)
            return cpu_ratio
        except subprocess.CalledProcessError:
            log.error("Tried to find cgroup size for " + self.cpu_cgroup_name + ", but couldn't find it\n")
            return None

    async def _inject_burst(self):
        log.info("Fault %s commencing burst\n" % (self.tag))

        burst_config = self.fault_pattern_args
        if len(burst_config) < 2:
            log.error(f"{self.tag} burst is missing parameters, defaulting to 1 second per 2 seconds\n")
            burst_duration = 1
            burst_period = 2
        else:
            burst_duration = int(burst_config[0]) / 1000
            burst_period = int(burst_config[1]) / 1000  # after each burst, wait for (burst_period - burst_duration)

        burst_num = int((self.injection_time) / burst_period)  # how often we burst

        if self.fault_type == 'custom':
            if len(self.fault_args) >= 2:
                start_command = self.fault_args[0]
                end_command = self.fault_args[1]
            elif len(self.fault_args) >= 1:
                start_command = self.fault_args[0]
                end_command = None
            else:
                log.error(f"{self.tag} missing fault args for injection\n")
                start_command = None
                end_command = None

            for _ in range(burst_num):
                self.execute_command_for_node(self.target_process_pid, start_command, True)
                await asyncio.sleep(burst_duration)
                self.execute_command_for_node(self.target_process_pid, end_command, False)
                await asyncio.sleep(burst_period - burst_duration)

        elif self.fault_type == 'stress_cpu':
            burst_duration = int(max(1, burst_duration))  # stress-ng has a minimum interval of 1 second
            cgroup_fraction = self._get_cgroup_size()
            if len(self.fault_args) >= 1:
                cpu_stress_percentage = int(self.fault_args[0])
            else:
                log.error(f"{self.tag} is missing stress intensity, defaulting to 50%")
                cpu_stress_percentage = 50

            stress_percentage_applied_to_cgroup = cpu_stress_percentage * cgroup_fraction
            num_cpus = math.ceil(stress_percentage_applied_to_cgroup / 100)
            stress_per_cpu = int(stress_percentage_applied_to_cgroup / num_cpus) if num_cpus > 0 else 0

            # decimal64 gives usages which are relatively close to the requested usage, unlike e.g. euler
            stress_command = f"stress-ng -l {stress_per_cpu} -t {burst_duration} --cpu {num_cpus} --cpu-method int64longdouble &"

            for _ in range(burst_num):
                self.execute_command_for_node(self.target_process_pid, stress_command, True)
                await asyncio.sleep(burst_duration)
                # Dummy call, to log that the command is likely done
                self.execute_command_for_node(self.target_process_pid, None, False)
                await asyncio.sleep(burst_period - burst_duration)

        else:
            log.error(f"{self.tag} has unknown fault type: {self.fault_type}\n")

    async def _inject_degradation(self):
        log.info("Fault %s commencing degradation\n" % (self.tag))

        if len(self.fault_pattern_args) >= 4:
            end_degradation = int(self.fault_pattern_args[3])
            # Don't limit to 100, since this isn't percentages
        else:
            end_degradation = 100
        if len(self.fault_pattern_args) >= 3:
            start_degradation = int(self.fault_pattern_args[2])
        else:
            start_degradation = 0
        if len(self.fault_pattern_args) >= 2:
            degradation_step_length = int(self.fault_pattern_args[1]) / 1000
        else:
            degradation_step_length = 1000 / 1000

        if len(self.fault_pattern_args) >= 1:
            degradation_step_size = int(self.fault_pattern_args[0])
        else:
            degradation_step_size = int(5)
            log.error(f"{self.tag} does not have enough pattern_args to define degradation step, defaulting to 5")

        number_of_steps = int(self.injection_time / degradation_step_length)
        injection_intensity = start_degradation

        if self.fault_type == 'custom':
            if len(self.fault_args) >= 2:
                start_base_command = self.fault_args[0]
                end_base_command = self.fault_args[1]
            elif len(self.fault_args) >= 1:
                start_base_command = self.fault_args[0]
                end_base_command = None
            else:
                log.error(f"{self.tag} missing fault args for injection\n")
                start_base_command = None
                end_base_command = None

            arguments_in_start_command = start_base_command.count("{}")
            if arguments_in_start_command > 1:
                # Iterating over multiple arguments is ill-defined, and running over arbitrary number of commands
                log.error(
                    f"{self.tag} contains more than one place to insert arguments, but currently only supports one!")

            for i in range(number_of_steps):
                start_command = start_base_command.format(injection_intensity)
                end_command = end_base_command

                self.execute_command_for_node(self.target_process_pid, start_command, True)
                await asyncio.sleep(degradation_step_length)
                self.execute_command_for_node(self.target_process_pid, end_command, False)

                injection_intensity = injection_intensity + degradation_step_size
                injection_intensity = min(injection_intensity, end_degradation)


        elif self.fault_type == 'stress_cpu':
            # increment by fault_pattern_args[0] every fault_pattern_args[1]
            cgroup_fraction = self._get_cgroup_size()
            # Run in the background with &, or this will be blocking logging
            stress_base_command = "stress-ng -l {} -t {} --cpu {} --cpu-method int64longdouble &"
            for i in range(number_of_steps):
                stress_to_inject = injection_intensity * cgroup_fraction
                num_cpus = math.ceil(stress_to_inject / 100)
                stress_per_cpu = int(stress_to_inject / num_cpus) if num_cpus > 0 else 0

                stress_command = stress_base_command.format(stress_per_cpu, int(degradation_step_length), num_cpus)
                self.execute_command_for_node(self.target_process_pid, stress_command, True)
                await asyncio.sleep(int(degradation_step_length))
                injection_intensity = int(injection_intensity + degradation_step_size)
                self.execute_command_for_node(self.target_process_pid, None, False)
                injection_intensity = min(injection_intensity, end_degradation)
        else:
            log.error(f"{self.tag} has unknown fault type: {self.fault_type}\n")

    async def _inject_persistent(self):
        log.info("Fault %s commencing persistent\n" % (self.tag))

        # Build up
        if self.fault_type == 'custom':
            duration_in_seconds = self.injection_time
            if len(self.fault_args) < 2:
                # Only start command is present
                start_command = self.fault_args[0]
                end_command = None  #
            elif len(self.fault_args) < 1:
                # No commands are present
                log.error(f"{self.tag} doesn't have enough arguments! ")
                start_command = None
                end_command = None
            else:
                # Start and stop command are present
                start_command = self.fault_args[0]
                end_command = self.fault_args[1]

            self.execute_command_for_node(self.target_process_pid, start_command, True)
            await asyncio.sleep(duration_in_seconds)
            self.execute_command_for_node(self.target_process_pid, end_command, False)

        elif self.fault_type == 'stress_cpu':
            # inject here
            # Users want n % cpu usage _on a node_ (=cgroup), but the stress-ng takes a global load.
            # To get our in-cpu we reduce the stress instruction by however much cpu is not allowed in our cgroup
            cgroup_fraction = float(self._get_cgroup_size())
            if len(self.fault_args) < 1:
                log.error(f"{self.tag} doesn't define stress intensity! defaulting to 50%")
                cpu_stress_percentage = 50
            else:
                cpu_stress_percentage = float(self.fault_args[0])
            stress_percentage_applied_to_cgroup = cpu_stress_percentage * cgroup_fraction
            num_cpus = math.ceil(stress_percentage_applied_to_cgroup / 100)
            stress_per_cpu = int(stress_percentage_applied_to_cgroup / num_cpus) if num_cpus > 0 else 0
            duration_in_seconds = self.injection_time
            stress_base_command = f"stress-ng -l {stress_per_cpu} -t {duration_in_seconds} --cpu {num_cpus} --cpu-method int64longdouble &"
            self.execute_command_for_node(self.target_process_pid, stress_base_command, True)
            await asyncio.sleep(duration_in_seconds)
            # No need to sleep, command runs for as long as indicated
            # Dummy call to log that it's done
            self.execute_command_for_node(self.target_process_pid, None, False)
        else:
            log.error(f"{self.tag} unknown fault type: {self.fault_type}")

    async def do_injection(self):
        log.info("Fault %s waits %s s of pre-injection time\n" % (self.tag, self.pre_injection_time))
        await asyncio.sleep(self.pre_injection_time)
        if self.fault_pattern == 'burst':
            await self._inject_burst()
        elif self.fault_pattern == 'degradation':
            await self._inject_degradation()
        elif self.fault_pattern == 'persistent':
            await self._inject_persistent()
        else:
            log.error(f"{self.tag} has unknown fault pattern")

        log.info("Fault %s waits %s s of post-injection time\n" % (self.tag, self.post_injection_time))
        await asyncio.sleep(self.post_injection_time)
        return
