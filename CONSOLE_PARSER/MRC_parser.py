# -*- coding: utf-8 -*-

from __future__ import print_function

import os
import sys
import argparse

import time
import stat
import re
import select
from collections import defaultdict
from collections import Counter

#from operator import itemgetter
import yaml
import json

import serial
import tty
import termios

# For LED highlighting using PCA9685
import smbus
import pca9685pw
from rmt import RMT

from benchmark.test_result import BasicTestResult
from benchmark.conf import Conf, parse_list

sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', 0)

CONF_FILE = 'MRC_parser.ini'

# Intel MRC base blocks
MRC_BBLOCK_START_RE = re.compile(r'START_([0-9A-Z_]+)')
MRC_BBLOCK_END_RE = re.compile(r'STOP_([0-9A-Z_]+)')

# Intel MRC iMC blocks functions
MRC_iMC_BLOCK_START_RE = re.compile(r'(^[A-Z].*) -- Started')
MRC_iMC_BLOCK_END_RE = re.compile(r'(^[A-Z].*) [-]?[=]? ([0-9]+)[ ]?ms')

# Intel SMM handlers sample code
MRC_SMM_BLOCK_START_RE = re.compile(r'(.*) Hander start!')
MRC_SMM_BLOCK_END_RE = re.compile(r'(.*) Hander end!')

# MRC Fatal Error
MRC_FATAL_ERROR_RE = re.compile(r'Major Code = [0-9]+, Minor Code = [0-9]+')

# Checkpoint regexp (POST codes):
#Checkpoint Code: Socket 0, 0xBF, 0x00, 0x0000
POST_CHECKPOINT_RE = re.compile(r'Checkpoint Code: Socket [01], (0x[0-9A-F]+), (0x[0-9A-F]+), (0x[0-9A-F]+)')

SERVER_POWER_ON_RE = re.compile(r'Status Code Available')
SERVER_POWER_OFF_RE = re.compile(r'SecSMI. S5 Trap')

# OS booted
RUNTIME_BLOCK_START_MARK = 'OSBootEvent = Success'
# SMM handler
SMM_BLOCK_MARK = 'SMM Error Handler Entry'
# SMBIOS data
SMMRC_BBLOCK_MARK = 'GenerateFruSmbiosData'

# Settings for PCA9685
PCA9685_I2C_BUS = 8 # bus id
PCA9685_I2C_ADDRESS = 0b1000000 # address pins [1][A5][A4][A3][A2][A1][A0]
LED_PWM_FREQ = 600 # hertz 64 recomended for Servos

# Led highlighting is turned off by default
LED_EXISTENCE = False

# For single socket platform
LED_DIMM_MATCH_TABLE = {
    'N0.C0.D0.R0' : 0,
    'N0.C0.D0.R1' : 0,
    'N0.C0.D1.R0' : 2,
    'N0.C0.D1.R1' : 2,
    'N0.C1.D0.R0' : 4,
    'N0.C1.D0.R1' : 4,
    'N0.C1.D1.R0' : 6,
    'N0.C1.D1.R1' : 6,
    'N0.C2.D0.R0' : 8,
    'N0.C2.D0.R1' : 8,
    'N0.C2.D1.R0' : 10,
    'N0.C2.D1.R1' : 10,
    'N0.C3.D0.R0' : 12,
    'N0.C3.D0.R1' : 12,
    'N0.C3.D1.R0' : 14,
    'N0.C3.D1.R1' : 14
}

DMIDECODE = { 
    'BIOS': {
        'Vendor': 'vendor',
        'Version': 'version',
        'Release Date': 'date',
        'BIOS Revision': 'revision',
    },  
    'Base Board': {
        'Manufacturer': 'vendor',
        'Product Name': 'model',
        'Serial Number': 'serial',
    },  
    'System': {
        'Manufacturer': 'vendor',
        'Product Name': 'model',
        'Serial Number': 'serial',
    },  
    'Processor': {
        'Manufacturer': 'vendor',
        'Family': 'family',
        'Version': 'model',
        'Max Speed': 'speed',
        'Serial Number': 'serial',
    },  
    'Memory': {
        'Locator': 'locator',
        'Type': 'type',
        'Speed': 'speed',
        'Configured Clock Speed': 'current speed',
        'Manufacturer': 'vendor',
        'Part Number': 'part number',
        'Form Factor': 'form factor',
        'Size': 'size',
        'Serial Number': 'serial',
        'Array Handle': 'array',
    },  
}


CLIENT_DESCRIPTION = """Yandex R&D Debug Log parser for Intel FFM DRAM Hard Error handlers"""
HELPS = {
    'source': 'source of test information',
    'config': 'config file path (default machinegun.ini)',
    'verbose': 'enable verbose output',
    'disable_sending': 'disable API calls and e-mail sending',
}

OPTIONS = { 
    'report': {
        # notification options
        'api_url': (str, 'https://benchmark-test.haas.yandex-team.ru/api'),
        'smtp_relay': (str, 'outbound-relay.yandex.net'),
        'mail_to': (parse_list, [])
    },  
    'signal_integrity': {
        'rmt_repeats' : (int, 5), 
        'guidelines' : (str, 'DDR4_Margin_guidelines.yaml')
    },
    'node_configuration': {
        'por_ram_freq' : (int, 2666),
        'dimms_count' : (int, 24),
        'sockets_count' : (int, 2),
        'channels_count' : (int, 6),
        'dimm_per_channel' : (int, 2)
    }
}

dimm_params = ['DIMM vendor', 'DRAM vendor', 'RCD vendor', 'Organisation', 'Form factor', 'Freq', 'Prod. week', 'PN', 'hex']

def tree():
    return defaultdict(tree)

ram_info = tree()


def argument_parsing():
    """
    Parse and return command line arguments
    """
    parser = argparse.ArgumentParser(description=CLIENT_DESCRIPTION)
    parser.add_argument('source', help=HELPS['source'])
    parser.add_argument('-c', '--config', help=HELPS['config'],
                        default=CONF_FILE)
    parser.add_argument('-v', '--verbose', help=HELPS['verbose'],
                        action='store_true')
    parser.add_argument('--disable-sending', help=HELPS['disable_sending'],
                        action='store_true', default=False)
    return parser.parse_args()

class TestResult(BasicTestResult):
    environment = {}
    def __init__(self, conf, name):
        """
        Initialization
        """
        self.name = name
        self.component = []
        self.config = conf[name]
        self.data = {'status': 'PASSED', 'errors': []}
        self.started_at = time.time()
        self.finished_at = None

    def save_to_file(self, filename):
        """
        Save test result to file
        """
        data_format = 'json'
        data2save = json.dumps(self.get_result_dict(), indent=4)
        tmpl = 'Saving test result to file {0} in {1} format'
        msg = tmpl.format(filename, data_format)
        print(msg)
        try:
            if filename == 'stdout':
                print(data2save)
            else:
                with open(filename, 'a') as fhandler:
                    fhandler.write(data2save)
        except (OSError, IOError) as err:
            print('Result saving error:' + {0})
            return False
        return True

def dbg_log_src_isconsole(dbg_log_data_source):
    if stat.S_ISCHR(os.stat(dbg_log_data_source).st_mode):
        return True;

def dbg_log_src_islogfile(dbg_log_data_source):
    if os.path.isfile(dbg_log_data_source) and os.path.getsize(dbg_log_data_source) > 0:
        return True;

def serial_data(port, baudrate):
    debug_console = serial.Serial(
        port=port,\
        baudrate=baudrate,\
        parity=serial.PARITY_NONE,\
        stopbits=serial.STOPBITS_ONE,\
        bytesize=serial.EIGHTBITS,\
        timeout=0)
    while True:
        yield debug_console.readline()
    debug_console.close()

def ident_dimm(device_rank, state):
    global LED_EXISTENCE
    if LED_EXISTENCE:
        severity_mapping = {
            'critical' : 100,
            'warning' : 20,
            }
        led_id = LED_DIMM_MATCH_TABLE[device_rank]
        if severity_mapping[state]:
            pwm = pca9685pw.Pca9685pw(8,PCA9685_I2C_BUS,PCA9685_I2C_ADDRESS)
            pwm.setPercent(led_id,severity_mapping[state])
        else:
            print("Can't find leds for highlighting failed DIMM")

def process_socket_info(dbg_log_block, dbg_block_name, socket_id):
    print("Processing Socket info table...")
    global ram_info
    header = ''
    param_id = 0
    socket_id_phrase = "Socket " + str(socket_id)
    for line in dbg_log_block:
        if line.startswith('=' * 10) or line.startswith('-' * 10)\
            or line.startswith('BDX'):
            continue
        line_stripped = ([v.strip() for v in line.split('|')])
        if line_stripped[0] == 'S':
            header = line_stripped
            continue
        if line_stripped[0] and line_stripped[0].isdigit():
            cs_id = 'Dimm ' + str(line_stripped[0])
            param_id = 0
        socket_dict = {}
        channel_dict = {}
        for index, channel_id in enumerate(header[1:-1]):
            if len(line_stripped[1:-1]) >= index + 1:
                if len(dimm_params) > param_id:
                    if len(line.split(':')) > 2:
                        value = line_stripped[index+1].split(':')[-1].strip()
                    else:
                        value = line_stripped[index+1].strip()
                    if dimm_params[param_id] == 'Freq':
                        speed_value_composed = line_stripped[index+1].split()
                        if len(speed_value_composed) == 2:
                            ram_info[socket_id_phrase][channel_id][cs_id]['Timings'] = speed_value_composed[-1]
                            value = speed_value_composed[0]

                    ram_info[socket_id_phrase][channel_id][cs_id][dimm_params[param_id]] = value
                else:
                    continue
        param_id += 1

def process_dimm_info(dbg_log_block, dbg_block_name, socket_id):
    print("Processing DIMM info table...")
    global ram_info
    ram_info_buffer = []
#    print(dbg_log_block)
    for line in dbg_log_block:
        #print(line)
        if line.startswith('=' * 10):
            continue
        ram_info_buffer.append([v.strip() for v in line.split('|')])

        header = ram_info_buffer[0][1:]
        for index, socket_id in enumerate(header):
              socket_dict = {}
              for line in ram_info_buffer[1:-1]:
                  if len(line) >= index + 2:
                      value = line[index+1].strip()
                      if not value or value == 'N/A':
                           continue
                      if line[0].startswith('Ch'):
                          channel_dict = {}
                          channel_raw, param = line[0].split()
                          channel_id = re.sub(r'Ch([0-3])', r"Channel \1", channel_raw)
                          #print("Channel ID: " + channel_id)
                          ram_info[socket_id][channel_id][param] = value
                      else:
                          key = line[0]
                          ram_info[socket_id][key] = value

def ram_info_completeness():
    print('Checking RAM info completeness...')
    ram_config_status = False
    global ram_info
    global conf
    node_configuration = conf['node_configuration']
    components = []
    components_counter = {
        'sockets_count' : 0,
        'channels_count' : 0,
        'dimms_count' : 0
    }
#    print("RAM_INFO:" + str(ram_info))
    # TODO: rewrite to list comprehension?
    for s, sconf in ram_info.items():
        if s.startswith('Socket'):
            components_counter['sockets_count'] += 1
            for c, chconf in sconf.items():
                if c.startswith('Channel'):
                    components_counter['channels_count'] += 1
                    for d, rdimm in chconf.items():
                        if isinstance(rdimm, dict) and rdimm['DIMM vendor'] != 'Not installed':
                            components_counter['dimms_count'] += 1
                            components.append({
                                'type': 'RAM',
                                'model': rdimm.get('PN'),
                                'vendor': rdimm.get('DRAM vendor'),
                                'size': rdimm.get('Organisation'),
                                'form factor': rdimm.get('Form factor'),
                                'speed': rdimm.get('Freq'),
                                'timings': rdimm.get('Timings'),
                            })
    # Validate fullness of ram_info data
    #print(Counter(value for values in ram_info.itervalues() for value in values))
    ram_config_status = all(components_counter[x] == node_configuration[x] for x in components_counter.keys())
    ram_rdimm_pns_set = set(dimm['model'] for dimm in components)

    return ram_config_status

def process_mbist(dbg_log_block, dbg_block_name, socket_id):
    print('MBIST_PROCESSING...')
    for line in dbg_log_block:
        #print(line)
        failed_rank_re = re.match(r'.*(N[0-9].C[0-6].D[0-3].R[0-9]): MemTest Failure!', line)
        if failed_rank_re:
            #failed_device = ''.join(e for e in failed_rank_re.group(1) if e.isalnum())
            #failed_device = ''.join(filter(str.isalnum, failed_rank_re.group(1)))
            failed_device = failed_rank_re.group(1)
            print('Founded DQ error in ' + failed_device)
            ident_dimm(failed_device,'warning')
        
def process_training_info(dbg_log_block, dbg_block_name, socket_id):
    for line in dbg_log_block:
        failed_rank_re = re.match(r'.*(N[0-9].C[0-6].D[0-3].R[0-9]).S[01][0-9]: Failed RdDqDqs', line)
        if failed_rank_re:
            failed_device = failed_rank_re.group(1)
            print('Founded training error ' + failed_device)
            ident_dimm(failed_device,'critical')

def process_smm_ce_handler(dbg_log_block, dbg_block_name, socket_id):
    print("Processing Runtime SMM handlers output...")
#    print(dbg_log_block)
    for line in dbg_log_block:
        failed_rank_re = re.match(r'Last Err Info Node=([0-9]) ddrch=([0-9]] dimm=([0]) rank=([1])', line)
        if failed_rank_re:
            failed_device = failed_rank_re.group(1)
            print('Founded training error ' + failed_device)
            ident_dimm(failed_device,'critical')

def parse_debug_log(result, args):
    # TODO: rewrite to class?
    """
    Parse Serial Debug Log for RDIMM/DRAM errors and call specific handlers 
    """
    #import pdb; pdb.set_trace()
    test_configuration = []
    environment = {}
    testplan = defaultdict(list)

    global rmt_dblock_counter
    rmt_dblock_counter = defaultdict(int)
    rmt_data_required = defaultdict(int)
    processed_funcs = []

    func_counter = defaultdict(int)

    block_buffer = defaultdict(list)
    block_processing_queue = []
    mrc_block_name = ''
    mrc_fatal_error_catched = False
    current_processing_block_ended = False

    rmt_instance = RMT(conf, ram_info, conf['signal_integrity']['guidelines'])

    if dbg_log_src_isconsole(args.source):
        print('Waiting for data from serial console' + args.source + '...')
        dbg_log_data = serial_data(args.source, 115200)
    if dbg_log_src_islogfile(args.source):
        dbg_log_data = open(args.source)

    dbg_block_processing_rules = { 
            'DIMMINFO_TABLE' : process_dimm_info,
            'SOCKET_0_TABLE' : process_socket_info,
            'SOCKET_1_TABLE' : process_socket_info,
            'BSSA_RMT' : rmt_instance.process_rmt_results,
            'RMT_N0' : rmt_instance.process_rmt_results,
            'RMT_N1' : rmt_instance.process_rmt_results,
            'Rx Dq/Dqs Basic' : process_training_info,
#            'MemTest' : process_mbist,
            'Corrected Memory Error' : process_smm_ce_handler
    }

    def console_data_dummy():
        return False

    # Goal testplan and processors dependencies rules
    testplan = {
        ram_info_completeness : [ process_socket_info, process_dimm_info ],
        rmt_instance.result_completeness : [ ram_info_completeness ],
        rmt_instance.qualification : [ rmt_instance.result_completeness, ram_info_completeness ],
        process_socket_info : [ console_data_dummy ],
        process_dimm_info : [ console_data_dummy ]
    }
    testplan_set = dict((k, set(testplan[k])) for k in testplan)
    
    def resolve_dependecies(testplan_set, processed_funcs):
        #import pdb; pdb.set_trace()
        #print("TESTPLAN_GEN_DICT: " + str(testplan_set))
        print("PROCESSED_FUNCS: " + str(set(processed_funcs)))
        # values not in keys (items without dep)
        funcs_wo_deps=set(i for v in testplan_set.values() for i in v)-set(testplan_set.keys())

        for p in processed_funcs:
            for k in testplan_set.keys():
                if k == p:
                    print("SETPLAN_P=" + str(testplan_set[k]))
                    testplan_set.pop(k, None)


#        print("ITEMS_WO_DEPS_VALUES: " + str(funcs_wo_deps) + " TYPE: " + str(type(funcs_wo_deps)))
        # and keys without value (items without dep)
        funcs_wo_deps.update(k for k, v in testplan_set.items() if not v)
#        print("ITEMS_WO_DEPS_KEYS: " + str(funcs_wo_deps))
#        print("ITEMS_TO_DO: " + str(funcs_wo_deps))
#        print("PROCESSED_FUNCS: " + str(processed_funcs))
#        print("ITEMS_TO_DO(STILL): " + str(funcs_wo_deps) + "; LENGHT: " + str(len(funcs_wo_deps)))
        testplan_set=dict(((k, v-set(processed_funcs)) for k, v in testplan_set.items() if v))
        processed_funcs = []
#        print("TESTPLAN_GEN_DICT_CLEANED: " + str(testplan_set))
        if len(funcs_wo_deps) != 0:
            for supplementary_func in funcs_wo_deps:
                if supplementary_func():
                    print("PASS: " + str(supplementary_func))
                    processed_funcs.append(supplementary_func)
        return testplan_set, processed_funcs

    print('Parsing for data from file ' + args.source + '...')

    for line in dbg_log_data:
        ansi_escape = re.compile(r'\x1B\[[0-?]*[ -/]*[@-~]')
        ansi_escape.sub('', line)

        line = line.rstrip('\r\n')
        if dbg_log_src_isconsole(args.source):
            if len(line) == 0:
                print('.', end='')
                time.sleep(0.3)
                continue

            print('*', end='')

        dbg_block_name = ''
        if MRC_BBLOCK_START_RE.match(line):
            dbg_block_name = MRC_BBLOCK_START_RE.match(line).group(1)
            dbg_block_end_re = MRC_BBLOCK_END_RE

        if MRC_iMC_BLOCK_START_RE.match(line):
            dbg_block_name = MRC_iMC_BLOCK_START_RE.match(line).group(1)
            dbg_block_end_re = MRC_iMC_BLOCK_END_RE
            #print(dbg_block_name)

        if MRC_SMM_BLOCK_START_RE.match(line):
            dbg_block_name = MRC_SMM_BLOCK_START_RE.match(line).group(1)
            dbg_block_end_re = MRC_SMM_BLOCK_END_RE

        if dbg_block_name:
            try:
                block_processor_name = dbg_block_processing_rules[dbg_block_name]
                block_processing_queue.append({dbg_block_name:dbg_block_end_re})
#                print("ADDED BLOCK: " + str(block_processing_queue))
            except KeyError as e:
                pass

        else:
            #print(line)
            if block_processing_queue:
                current_processing_block_ended = False
                if MRC_FATAL_ERROR_RE.match(line):
                    mrc_fatal_error_catched = True
                current_processing_block_name = ''.join(block_processing_queue[-1].keys())
#                print("CURRENT_BLOCK: " + str(current_processing_block_name))
                current_processing_block_end_re = block_processing_queue[-1][current_processing_block_name]
#                print(current_processing_block_end_re.pattern)
                founded_stop_block_mark = current_processing_block_end_re.match(line)
                if founded_stop_block_mark:
#                    print("STOP_BLOCK_LINE: " + line)
#                    print(founded_stop_block_mark.group(1))
#                    print(current_processing_block_name)
                    if founded_stop_block_mark.group(1) == current_processing_block_name:
                        current_processing_block_ended = True
                if mrc_fatal_error_catched or current_processing_block_ended:
#                    print(current_processing_block_name)
                    if dbg_block_processing_rules[current_processing_block_name]:
                        func = dbg_block_processing_rules[current_processing_block_name]
                        socket_id = re.sub(r'\D', "", current_processing_block_name)
                        if not socket_id:
                            socket_id = None
                        #print(block_buffer[current_processing_block_name])
                        func(block_buffer[current_processing_block_name], current_processing_block_name, socket_id)
                        processed_funcs.append(func)
#                        print("BEFORE: " + str(block_processing_queue[-1].keys()))
                        block_processing_queue.pop()
                        mrc_fatal_error_catched = False
                        # Check for possibility to run supplimentary functions and execute them if possible
                        if testplan.keys():
                            testplan_set, processed_funcs = resolve_dependecies(testplan_set, processed_funcs)
                        else:
                            break
                else:
                    block_buffer[current_processing_block_name].append(line)

    n = 0
    print("LAST_CHANCE: " + str(testplan_set))
    testplan_set.pop(console_data_dummy, None)
    while testplan_set:
        testplan_set, processed_funcs = resolve_dependecies(testplan_set, processed_funcs)
        n += 1
        if n > 10:
            print("Failed! Not enought data for accomplishing the goals!")
            break

#    print('Configuration check:')
    #result.component = get_ram_config(ram_info)

    #environment['baseboard'] = baseboard_mfg + " " + baseboard_product
    #environment['inventory'] = baseboard_serial
    #environment['bmc version'] = bmc_version.lstrip('0')


def main():
    """
    The main function
    """
    args = argument_parsing()
    test_name = 'signal_integrity'
    global conf
    conf = Conf(OPTIONS, args.config, log=False)
    result = TestResult(conf, test_name)
    global LED_EXISTENCE
    
    try:
        pwm = pca9685pw.Pca9685pw(8,PCA9685_I2C_BUS,PCA9685_I2C_ADDRESS)
        pwm.defaultAddress = PCA9685_I2C_ADDRESS
        pwm.setFrequency(LED_PWM_FREQ)
        pwm.reset()
        LED_EXISTENCE = True
        for i in range(0,16):
          pwm.setFullOff(i)
    except IOError as err:
        print("Warning! Can't find leds for highlighting failed DIMM")


    parse_debug_log(result, args)
#    print(json.dumps(ram_info, indent=2))
#    print(json.dumps(result.get_result_dict(), indent=2))

#    if not args.disable_sending:
#        result.send_via_api(conf['report']['api_url'])


if __name__ == '__main__':
    main()
# vim: tabstop=8 softtabstop=0 expandtab shiftwidth=4 smarttab
